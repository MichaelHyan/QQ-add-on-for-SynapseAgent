from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable, Optional

from .agent_client import AgentClient, AgentError
from .handler import IncomingMessage

log = logging.getLogger(__name__)

# 标签用拼接方式构造，避免源码中出现完整的调用标签字面量
_TAG_OPEN = "<" + "tool_call" + ">"
_TAG_CLOSE = "</" + "tool_call" + ">"
_TOOL_CALL_RE = re.compile(_TAG_OPEN + r".*?" + _TAG_CLOSE, re.DOTALL)

_EMPTY_HINT = "（请输入具体内容）"
_NO_RESPONSE_HINT = "（Agent 未返回内容，请稍后重试）"
_TIMEOUT_HINT = "（响应超时，请稍后重试）"
_AGENT_DOWN_HINT = "（Agent 暂时不可用，请稍后重试）"
_TRUNCATED_HINT = "\n...（内容过长，已截断）"

# QQ 被动回复对同一 msg_id 的 msg_seq 最多到 5 次，留 1 次余量。
# 这是平台硬约束，不是设计选择：超出的内容发不出去。
MAX_REPLY_SEGMENTS = 4
# 单条 QQ 消息安全上限（handler 里还有 1800 兜底）
SEGMENT_LIMIT = 1500
# 首次拿到内容的宽限期（秒），超过则判定 Agent 无响应
FIRST_REPLY_TIMEOUT = 60.0
# 合并窗口（秒）：收到新内容后重新计时，空闲超过该时长才发出
MERGE_IDLE_SECONDS = 15.0

SendReply = Callable[[IncomingMessage, str], Awaitable[None]]


def strip_tool_calls(text: str) -> str:
    """移除 Agent 输出中的工具调用标签，避免原样发给用户。"""
    if not text:
        return ""
    return _TOOL_CALL_RE.sub("", text).strip()


def split_text(text: str, limit: int = SEGMENT_LIMIT) -> list[str]:
    """按长度切分文本，尽量在换行或句号处断开。"""
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = -1
        for sep in ("\n\n", "\n", "。", "！", "？", ". ", "; ", "；"):
            pos = window.rfind(sep)
            if pos > limit // 3:
                cut = pos + len(sep)
                break
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return [c for c in chunks if c]


class AgentBridge:
    """QQ 消息与后端 Agent 之间的桥接。

    单会话串行：同一时刻只有一条消息在等待 Agent 回复。

    投递格式（include_author=True 时）：

        用户8f3a：你好

    群聊/单聊拿不到昵称，用 openid 短哈希代替；频道消息有 username 就直接用。

    回复策略：**滑动窗口合并**。
      - answer 每返回一段非空内容，追加进缓冲区（换行连接）
      - 每次追加都把"空闲截止时间"重置为 now + merge_idle
      - 空闲超过 merge_idle，或本轮结束（status=0），才把缓冲区整体发出

    轮询语义：
      - status=1          → Agent 仍在生成
      - status=0 且有内容 → 本轮结束
      - status=0 且无内容 → Agent 尚未开始产出，继续等待（受首响应宽限期约束）
    """

    def __init__(
        self,
        client: AgentClient,
        send_reply: SendReply,
        max_queue: int = 32,
        pending_timeout: float = 300.0,
        first_reply_timeout: float = FIRST_REPLY_TIMEOUT,
        merge_idle: float = MERGE_IDLE_SECONDS,
        max_segments: int = MAX_REPLY_SEGMENTS,
        include_author: bool = True,
    ) -> None:
        self._client = client
        self._send_reply = send_reply
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue(
            maxsize=max(1, int(max_queue))
        )
        self._pending: Optional[IncomingMessage] = None
        self._pending_since: float = 0.0
        self._pending_timeout = max(float(pending_timeout), 10.0)
        self._first_reply_timeout = max(float(first_reply_timeout), 1.0)
        self._merge_idle = max(float(merge_idle), 0.1)
        self._max_segments = max(1, int(max_segments))
        self._include_author = bool(include_author)

        self._buffer: list[str] = []
        self._flush_deadline: float = 0.0
        self._got_content = False
        self._sent_segments = 0
        self._truncated = False

        self._idle = asyncio.Event()
        self._idle.set()
        self._connected = True
        self._closed = False
        self._tasks: list[asyncio.Task] = []

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._dispatch_loop(), name="agent-dispatch"),
            asyncio.create_task(self._poll_loop(), name="agent-poll"),
        ]
        log.info(
            "Agent 桥接已启动（endpoint=%s 轮询=%.1fs 首响应宽限=%.0fs "
            "合并窗口=%.0fs 最多%d条 发言者前缀=%s）",
            self._client.endpoint, self._client.poll_interval,
            self._first_reply_timeout, self._merge_idle, self._max_segments,
            "开" if self._include_author else "关",
        )

    async def stop(self) -> None:
        self._closed = True
        self._idle.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        log.info("Agent 桥接已停止")

    # ---------------- 入队 ----------------

    def submit(self, message: IncomingMessage) -> bool:
        """由 MessageHandler 回调，把消息放入待投递队列。"""
        if self._closed:
            return False
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            log.warning(
                "队列已满，丢弃消息 [%s] target=%s",
                message.kind, message.target_id,
            )
            return False
        log.info(
            "消息入队 [%s] target=%s 队列长度=%d",
            message.kind, message.target_id, self._queue.qsize(),
        )
        return True

    # ---------------- 内部状态 ----------------

    def _reset_round(self) -> None:
        self._buffer = []
        self._flush_deadline = 0.0
        self._got_content = False
        self._sent_segments = 0
        self._truncated = False

    def _set_pending(self, message: IncomingMessage) -> None:
        self._pending = message
        self._pending_since = time.monotonic()
        self._reset_round()
        self._idle.clear()

    def _clear_pending(self) -> None:
        if self._pending is not None:
            log.debug("释放 pending 消息 target=%s", self._pending.target_id)
        self._pending = None
        self._pending_since = 0.0
        self._reset_round()
        self._idle.set()

    def _buffered_text(self) -> str:
        """缓冲区内容用换行连接。"""
        return "\n".join(self._buffer)

    def _append(self, text: str) -> None:
        """追加一段内容，并重置空闲计时。"""
        text = (text or "").strip()
        if not text:
            return
        self._buffer.append(text)
        self._got_content = True
        self._flush_deadline = time.monotonic() + self._merge_idle

    def _format_payload(self, message: IncomingMessage, content: str) -> str:
        """给消息加发言者前缀（仅群聊生效）。

        单聊/频道/频道私信一律不加前缀，与 agent_include_author 开关无关：
        这些场景要么是单人对话，要么加前缀反而干扰 Agent 判断上下文。
        """
        if message.kind != "group" or not self._include_author:
            return content
        name = (message.author_name or "").strip()
        if not name:
            return content
        return f"{name}：{content}"

    # ---------------- 发送 ----------------

    async def _deliver(self, message: IncomingMessage, text: str) -> None:
        """发送一条并计数。"""
        try:
            await self._send_reply(message, text)
        except Exception:
            log.exception("发送 Agent 回复失败")
            return
        self._sent_segments += 1
        log.info(
            "已发送第 %d 段 [%s] target=%s len=%d",
            self._sent_segments, message.kind, message.target_id, len(text),
        )

    async def _flush(self, message: IncomingMessage) -> None:
        """把缓冲区内容发出，必要时切分并受条数上限约束。"""
        text = self._buffered_text().strip()
        if not text:
            return
        self._buffer = []
        self._flush_deadline = 0.0

        remaining = self._max_segments - self._sent_segments
        if remaining <= 0:
            if not self._truncated:
                self._truncated = True
                log.warning(
                    "已达单条消息回复条数上限（%d 条），后续内容丢弃 [%s] target=%s",
                    self._max_segments, message.kind, message.target_id,
                )
            return

        chunks = split_text(text)
        if len(chunks) <= remaining:
            for chunk in chunks:
                await self._deliver(message, chunk)
            return

        # 空间不够：留 1 条给截断提示
        if remaining >= 2:
            for chunk in chunks[: remaining - 1]:
                await self._deliver(message, chunk)
            await self._deliver(message, _TRUNCATED_HINT.strip())
        else:
            head = chunks[0][: SEGMENT_LIMIT - len(_TRUNCATED_HINT)].rstrip()
            await self._deliver(message, head + _TRUNCATED_HINT)
        self._truncated = True
        log.warning(
            "内容超长，已截断 [%s] target=%s", message.kind, message.target_id
        )

    # ---------------- 投递协程 ----------------

    async def _dispatch_loop(self) -> None:
        while not self._closed:
            message = await self._queue.get()
            try:
                # 串行：等上一条处理完
                while not self._closed and self._pending is not None:
                    await self._idle.wait()

                if self._closed:
                    return

                content = (message.content or "").strip()
                if not content:
                    log.info("收到空内容消息，跳过投递")
                    try:
                        await self._send_reply(message, _EMPTY_HINT)
                    except Exception:
                        log.exception("发送空内容提示失败")
                    continue

                payload = self._format_payload(message, content)

                self._set_pending(message)
                try:
                    await self._client.ask(payload)
                except AgentError as exc:
                    log.warning("投递 Agent 失败: %s", exc)
                    self._clear_pending()
                    try:
                        await self._send_reply(message, _AGENT_DOWN_HINT)
                    except Exception:
                        log.exception("发送失败提示失败")
                    continue

                log.info(
                    "已投递 Agent [%s] target=%s payload=%r",
                    message.kind, message.target_id, payload[:100],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("投递协程异常")
                self._clear_pending()
            finally:
                self._queue.task_done()

    # ---------------- 轮询协程 ----------------

    async def _poll_loop(self) -> None:
        while not self._closed:
            try:
                await asyncio.sleep(self._client.poll_interval)

                message = self._pending
                if message is None:
                    continue

                waited = time.monotonic() - self._pending_since

                # 总超时：Agent 卡住时不永久占用会话
                if waited > self._pending_timeout:
                    log.warning(
                        "等待 Agent 回复超时（%.0fs），放弃当前消息", waited,
                    )
                    await self._flush(message)
                    self._clear_pending()
                    try:
                        await self._send_reply(message, _TIMEOUT_HINT)
                    except Exception:
                        log.exception("发送超时提示失败")
                    continue

                # 首响应宽限期：从未拿到内容时，超过此值判定 Agent 无响应
                if not self._got_content and waited > self._first_reply_timeout:
                    log.warning(
                        "Agent 首次响应宽限期（%.0fs）耗尽，放弃当前消息",
                        self._first_reply_timeout,
                    )
                    self._clear_pending()
                    try:
                        await self._send_reply(message, _NO_RESPONSE_HINT)
                    except Exception:
                        log.exception("发送无响应提示失败")
                    continue

                try:
                    status, response = await self._client.answer()
                except AgentError as exc:
                    if self._connected:
                        log.warning("轮询 Agent 失败: %s", exc)
                        self._connected = False
                    continue

                if not self._connected:
                    log.info("Agent 连接已恢复")
                    self._connected = True

                self._append(strip_tool_calls(response))

                if status == 0:
                    if self._got_content:
                        # 有内容 + status=0：本轮真正结束，立即发出
                        await self._flush(message)
                        log.info(
                            "Agent 本轮结束（等待 %.1fs，共发 %d 段）",
                            waited, self._sent_segments,
                        )
                        self._clear_pending()
                    else:
                        # 无内容 + status=0：Agent 还没开始产出，继续等
                        log.debug(
                            "Agent 尚未产出内容（已等待 %.1fs），继续轮询", waited,
                        )
                        continue
                elif self._buffer and time.monotonic() >= self._flush_deadline:
                    # status=1 但已空闲超过合并窗口：先把已收到的发出去
                    await self._flush(message)
                    log.info(
                        "空闲 %.0fs，先发出已收到的内容", self._merge_idle
                    )

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("轮询协程异常")
                await asyncio.sleep(1.0)