from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

import aiohttp

from .api import APIError, QQBotAPI
from .media import build_media_parts, excerpt_media

log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@!?\d+>")
MAX_CONTENT_LEN = 1800  # QQ 消息长度上限约 2000，预留余量

# QQ 平台错误码：消息被去重（同一 msg_id + msg_seq 重复提交）
ERR_DUPLICATE_MSG = 40054005

# 媒体附件默认阈值
DEFAULT_MEDIA_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MEDIA_TOTAL_BYTES = 8 * 1024 * 1024
DEFAULT_MEDIA_TIMEOUT = 20.0


def clean_content(text: str) -> str:
    """去掉频道消息中的 @机器人 标记并压缩空白。"""
    if not text:
        return ""
    return _MENTION_RE.sub("", text).strip()


def make_display_name(raw_name: str, openid: str) -> str:
    """生成发言者显示名。

    频道事件自带 username；群聊/单聊只给不透明 openid，
    用其短哈希代替，保证同一人稳定可辨。
    """
    raw_name = (raw_name or "").strip()
    if raw_name:
        return raw_name
    openid = (openid or "").strip()
    if not openid:
        return "未知用户"
    digest = hashlib.md5(openid.encode("utf-8")).hexdigest()
    return "用户" + digest[:4]


@dataclass
class IncomingMessage:
    """统一后的入站消息（与具体事件类型解耦）。"""

    kind: str  # group / c2c / guild / dm
    event_type: str
    content: str
    target_id: str  # group_openid / user_openid / channel_id / guild_id
    msg_id: str
    author_id: str = ""
    author_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        """会话标识，后续接入 Agent 保持多轮上下文时可用。"""
        return f"{self.kind}:{self.target_id}:{self.author_id}"


class Backend(Protocol):
    """同步回复后端：固定回复等实现此协议。"""

    async def reply(self, message: IncomingMessage) -> Optional[str]:
        ...


class FixedReplyBackend:
    """固定回复实现。"""

    def __init__(self, text: str) -> None:
        self._text = text

    async def reply(self, message: IncomingMessage) -> Optional[str]:
        return self._text


MessageCallback = Callable[[IncomingMessage], Any]


class MessageHandler:
    """事件解析 + 回复分发。

    两种工作模式：
      - 固定回复：传入 backend，由 handler 直接回固定文本
      - Agent 模式：传入 on_message 回调，由外部桥接处理，handler 只负责解析

    媒体处理：若事件带 attachments，会把图片/音频/视频下载并 base64 编码，
    以 ``<image>...</image>`` 形式追加到消息内容，交给后端多模态解析。
    """

    _SUPPORTED = {
        "GROUP_AT_MESSAGE_CREATE",
        "C2C_MESSAGE_CREATE",
        "AT_MESSAGE_CREATE",
        "DIRECT_MESSAGE_CREATE",
    }
    _NOTICE = {
        "GROUP_ADD_ROBOT": "机器人被添加到群聊",
        "GROUP_DEL_ROBOT": "机器人被移出群聊",
        "FRIEND_ADD": "用户添加机器人",
        "FRIEND_DEL": "用户删除机器人",
        "GROUP_MSG_REJECT": "群管理关闭机器人主动消息",
        "GROUP_MSG_RECEIVE": "群管理开启机器人主动消息",
    }

    def __init__(
        self,
        api: QQBotAPI,
        backend: Optional[Backend] = None,
        on_message: Optional[MessageCallback] = None,
        session: Optional[aiohttp.ClientSession] = None,
        media_enabled: bool = True,
        media_max_bytes: int = DEFAULT_MEDIA_MAX_BYTES,
        media_total_bytes: int = DEFAULT_MEDIA_TOTAL_BYTES,
        media_timeout: float = DEFAULT_MEDIA_TIMEOUT,
        media_url_fallback: bool = True,
    ) -> None:
        self._api = api
        self._backend = backend
        self._on_message = on_message
        self._msg_seq: dict[str, int] = {}

        self._session = session
        self._media_enabled = bool(media_enabled)
        self._media_max_bytes = max(int(media_max_bytes), 1)
        self._media_total_bytes = max(int(media_total_bytes), 1)
        self._media_timeout = max(float(media_timeout), 1.0)
        self._media_url_fallback = bool(media_url_fallback)

    # ---------------- 装配 ----------------

    def set_backend(self, backend: Backend) -> None:
        self._backend = backend

    def set_message_callback(self, callback: Optional[MessageCallback]) -> None:
        self._on_message = callback

    # ---------------- 事件入口 ----------------

    async def on_event(self, event_type: str, data: dict) -> None:
        if event_type in self._NOTICE:
            log.info("通知事件 %s: %s", event_type, self._NOTICE[event_type])
            return

        message = self._parse(event_type, data)
        if message is None:
            log.debug("忽略事件 %s", event_type)
            return

        await self._attach_media(message, data)

        log.info(
            "收到消息 [%s/%s] author=%s(%s) target=%s content=%r",
            message.kind, message.event_type, message.author_name,
            message.author_id, message.target_id,
            excerpt_media(message.content),
        )

        # Agent 模式：交给外部桥接（入队后异步回复）
        if self._on_message is not None:
            try:
                self._on_message(message)
            except Exception:
                log.exception("消息回调执行失败")
            return

        # 固定回复模式
        if self._backend is None:
            log.warning("未配置回复后端，忽略消息")
            return

        try:
            reply = await self._backend.reply(message)
        except Exception:
            log.exception("后端处理消息失败")
            return

        if not reply:
            log.info("后端返回空内容，跳过回复")
            return

        await self.send_reply(message, reply)

    # ---------------- 媒体处理 ----------------

    async def _attach_media(self, message: IncomingMessage, data: dict) -> None:
        """把附件下载编码后追加到消息内容。"""
        if not self._media_enabled or self._session is None:
            return

        attachments = data.get("attachments") or []
        if not attachments:
            return

        try:
            parts = await build_media_parts(
                self._session,
                attachments,
                max_bytes=self._media_max_bytes,
                total_bytes=self._media_total_bytes,
                timeout=self._media_timeout,
                url_fallback=self._media_url_fallback,
            )
        except Exception:
            log.exception("媒体处理异常，按纯文本继续")
            return

        if not parts:
            return

        extra = "\n".join(parts)
        message.content = (message.content + "\n" + extra) if message.content else extra
        log.info(
            "已附加 %d 个媒体片段，内容长度 %d", len(parts), len(message.content)
        )

    # ---------------- 解析 ----------------

    def _parse(self, event_type: str, data: dict) -> Optional[IncomingMessage]:
        if event_type not in self._SUPPORTED:
            return None

        author = data.get("author") or {}

        if event_type == "GROUP_AT_MESSAGE_CREATE":
            author_id = author.get("member_openid", "")
            return IncomingMessage(
                kind="group",
                event_type=event_type,
                content=clean_content(data.get("content", "")),
                target_id=data.get("group_openid", ""),
                msg_id=data.get("id", ""),
                author_id=author_id,
                author_name=make_display_name(author.get("username", ""), author_id),
                raw=data,
            )

        if event_type == "C2C_MESSAGE_CREATE":
            author_id = author.get("user_openid", "")
            return IncomingMessage(
                kind="c2c",
                event_type=event_type,
                content=clean_content(data.get("content", "")),
                target_id=author_id,
                msg_id=data.get("id", ""),
                author_id=author_id,
                author_name=make_display_name(author.get("username", ""), author_id),
                raw=data,
            )

        if event_type == "AT_MESSAGE_CREATE":
            author_id = author.get("id", "")
            return IncomingMessage(
                kind="guild",
                event_type=event_type,
                content=clean_content(data.get("content", "")),
                target_id=data.get("channel_id", ""),
                msg_id=data.get("id", ""),
                author_id=author_id,
                author_name=make_display_name(author.get("username", ""), author_id),
                raw=data,
            )

        if event_type == "DIRECT_MESSAGE_CREATE":
            author_id = author.get("id", "")
            return IncomingMessage(
                kind="dm",
                event_type=event_type,
                content=clean_content(data.get("content", "")),
                target_id=data.get("guild_id", ""),
                msg_id=data.get("id", ""),
                author_id=author_id,
                author_name=make_display_name(author.get("username", ""), author_id),
                raw=data,
            )

        return None

    # ---------------- 回复 ----------------

    def _next_seq(self, msg_id: str) -> int:
        seq = self._msg_seq.get(msg_id, 0) + 1
        if len(self._msg_seq) > 2000:  # 简单防膨胀
            self._msg_seq.clear()
        self._msg_seq[msg_id] = seq
        return seq

    async def send_reply(self, message: IncomingMessage, text: str) -> None:
        """按消息类型发送文本（被动回复，带 msg_id）。"""
        if len(text) > MAX_CONTENT_LEN:
            text = text[: MAX_CONTENT_LEN - 3] + "..."

        seq = self._next_seq(message.msg_id)
        try:
            if message.kind == "group":
                await self._api.send_group_message(
                    message.target_id, text, message.msg_id, seq
                )
            elif message.kind == "c2c":
                await self._api.send_c2c_message(
                    message.target_id, text, message.msg_id, seq
                )
            elif message.kind == "guild":
                await self._api.send_channel_message(
                    message.target_id, text, message.msg_id
                )
            elif message.kind == "dm":
                await self._api.send_dms_message(
                    message.target_id, text, message.msg_id
                )
            else:
                log.warning("未知消息类型 %s，未发送回复", message.kind)
                return
            log.info("已回复 [%s] -> %s (seq=%d)", message.kind, message.target_id, seq)
        except APIError as exc:
            if exc.code == ERR_DUPLICATE_MSG:
                # 平台去重保护：该 msg_id 的这条回复已经提交过
                # 常见原因：多实例同时在线，或事件被网关重推
                log.warning(
                    "消息被去重，跳过重复回复 [%s] target=%s msg_id=%s seq=%d",
                    message.kind, message.target_id, message.msg_id, seq,
                )
                return
            log.error(
                "回复消息失败 [%s] target=%s status=%s code=%s body=%s",
                message.kind, message.target_id, exc.status, exc.code, exc.body[:300],
            )
        except Exception:
            log.exception(
                "回复消息失败 [%s] target=%s", message.kind, message.target_id
            )

    # 兼容旧调用名
    _send_reply = send_reply