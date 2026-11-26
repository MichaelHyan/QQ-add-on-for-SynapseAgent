from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

import aiohttp

from .api import APIError, QQBotAPI
from .commands import method as handle_command
from .files import (
    DEFAULT_FILE_MAX_BYTES,
    download_to_file,
    extract_file_tags,
    file_type_of,
    safe_name,
    to_base64,
)
from .media import build_media_parts, detect_tag, excerpt_media

log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@!?\d+>")
MAX_CONTENT_LEN = 1800  # QQ 消息长度上限约 2000，预留余量

# QQ 平台错误码：消息被去重（同一 msg_id + msg_seq 重复提交）
ERR_DUPLICATE_MSG = 40054005

# 媒体附件默认阈值
DEFAULT_MEDIA_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MEDIA_TOTAL_BYTES = 8 * 1024 * 1024
DEFAULT_MEDIA_TIMEOUT = 20.0

# 文件下载默认超时（大文件给足时间）
DEFAULT_FILE_TIMEOUT = 120.0


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

    媒体处理：图片/音频/视频下载后 base64 编码，以 agent_* 标签追加到内容。

    文件处理：
      - 收：非媒体附件流式落盘到 file_dir，内容追加
        ``[收到文件] 原文件名 -> 绝对路径``；群聊/单聊/频道私信都落盘
      - 发：回复文本里的 ``<agent_file>路径</agent_file>`` 先发文字，
        再把文件上传后以 msg_type=7 发出

    附件降级规则（保证既有行为不变）：
      - 媒体 + 文件功能都关闭 → 完全不追加，直接返回
      - 仅文件功能开启 → 媒体附件降级为 ``[附件] 名`` 占位
      - 文件功能关闭 → 普通附件降级为 ``[附件] 名`` 占位
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
        file_enabled: bool = False,
        file_dir: str = "",
        file_max_bytes: int = DEFAULT_FILE_MAX_BYTES,
        file_send_max_bytes: int = DEFAULT_FILE_MAX_BYTES,
        file_timeout: float = DEFAULT_FILE_TIMEOUT,
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

        self._file_enabled = bool(file_enabled)
        self._file_dir = str(file_dir or "")
        self._file_max_bytes = max(int(file_max_bytes), 0)
        self._file_send_max_bytes = max(int(file_send_max_bytes), 0)
        self._file_timeout = max(float(file_timeout), 1.0)

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

        # 固定指令通道：纯信息部分以 / 开头时交给本地指令处理，
        # 不下载附件、也不投递给后端 Agent。fixed / agent 两种模式都生效。
        command_reply = self._dispatch_command(message)
        if command_reply is not None:
            if command_reply:
                await self.send_reply(message, command_reply)
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

    # ---------------- 固定指令通道 ----------------

    def _dispatch_command(self, message: IncomingMessage) -> Optional[str]:
        """尝试走固定指令通道。

        返回 ``None`` 表示不是指令，调用方继续原流程；返回字符串表示已处理，
        内容非空时作为回复发出。
        """
        try:
            return handle_command(message.content, message)
        except Exception:
            log.exception("指令通道异常，按普通消息继续处理")
            return None

    # ---------------- 附件处理 ----------------

    async def _attach_media(self, message: IncomingMessage, data: dict) -> None:
        """把附件追加到消息内容（媒体 base64 / 普通文件落盘）。"""
        attachments = data.get("attachments") or []
        if not attachments or self._session is None:
            return
        # 两个功能都关：不追加任何内容，保持既有行为
        if not self._media_enabled and not self._file_enabled:
            return

        media_atts: list[Any] = []
        file_atts: list[Any] = []
        labels: list[str] = []

        for att in attachments:
            if not isinstance(att, dict):
                continue
            content_type = att.get("content_type") or att.get("contentType") or ""
            url = att.get("url") or ""
            filename = att.get("filename") or att.get("file_name") or ""
            if detect_tag(content_type, filename, url) is not None:
                if self._media_enabled:
                    media_atts.append(att)
                else:
                    labels.append("[附件] %s" % (filename or url))
            else:
                file_atts.append(att)

        parts: list[str] = []

        if media_atts:
            try:
                parts.extend(
                    await build_media_parts(
                        self._session,
                        media_atts,
                        max_bytes=self._media_max_bytes,
                        total_bytes=self._media_total_bytes,
                        timeout=self._media_timeout,
                        url_fallback=self._media_url_fallback,
                    )
                )
            except Exception:
                log.exception("媒体处理异常，跳过媒体片段")

        parts.extend(labels)

        for att in file_atts:
            url = att.get("url") or ""
            filename = att.get("filename") or att.get("file_name") or "file"
            if not self._file_enabled or not url:
                label = filename or url
                if label:
                    parts.append("[附件] %s" % label)
                continue
            path, size = await download_to_file(
                self._session,
                url,
                filename,
                message.msg_id,
                self._file_dir,
                max_bytes=self._file_max_bytes,
                timeout=self._file_timeout,
            )
            if path:
                parts.append("[收到文件] %s -> %s" % (safe_name(filename), path))
                log.info(
                    "已保存文件 [%s] %s（%d 字节）-> %s",
                    message.kind, filename, size, path,
                )
            else:
                parts.append("[附件] %s" % (filename or url))
                log.warning("文件保存失败 [%s] %s", message.kind, filename)

        if not parts:
            return

        extra = "\n".join(parts)
        message.content = (message.content + "\n" + extra) if message.content else extra
        log.info(
            "已附加 %d 个附件片段，内容长度 %d", len(parts), len(message.content)
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
        """按消息类型发送回复：先发文字，再发 ``<agent_file>`` 指定的文件。

        文件标签在执行时被移除，不会出现在文字内容里。
        """
        paths, plain = extract_file_tags(text or "")
        if plain:
            await self._send_text(message, plain)
        for path in paths:
            await self._send_file(message, path)

    async def _send_text(self, message: IncomingMessage, text: str) -> None:
        """发送纯文本（被动回复，带 msg_id）。"""
        if not text:
            return
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

    async def _send_file(self, message: IncomingMessage, path: str) -> None:
        """上传并发送一个本地文件（群聊 / 单聊支持，频道类跳过）。"""
        if not self._file_enabled:
            log.info("文件发送未启用，跳过 %s", path)
            return

        if message.kind not in ("group", "c2c"):
            log.warning(
                "该消息类型不支持发文件 [%s]，跳过 %s", message.kind, path
            )
            return

        path = str(path or "").strip()
        if not path or not os.path.isfile(path):
            log.warning("文件不存在，跳过发送：%r", path)
            return

        try:
            size = os.path.getsize(path)
        except OSError:
            log.warning("无法读取文件大小，跳过发送：%r", path)
            return

        limit = self._file_send_max_bytes or self._file_max_bytes
        if limit and size > limit:
            log.warning(
                "文件超过发送上限（%d > %d 字节），跳过：%s", size, limit, path
            )
            return

        file_type = file_type_of(path)
        try:
            data = to_base64(path)
        except OSError as exc:
            log.warning("读取文件失败，跳过发送：%s（%s）", path, exc)
            return

        try:
            file_name = os.path.basename(path)
            if message.kind == "group":
                uploaded = await self._api.upload_group_file(
                    message.target_id, data, file_type,
                    file_name=file_name,
                )
            else:
                uploaded = await self._api.upload_c2c_file(
                    message.target_id, data, file_type,
                    file_name=file_name,
                )
        except Exception:
            log.exception("上传文件失败 [%s] target=%s path=%s",
                          message.kind, message.target_id, path)
            return

        file_info = ""
        if isinstance(uploaded, dict):
            file_info = uploaded.get("file_info") or ""
        if not file_info:
            log.warning(
                "上传结果缺少 file_info，跳过发送 [%s] target=%s path=%s resp=%r",
                message.kind, message.target_id, path, str(uploaded)[:200],
            )
            return

        seq = self._next_seq(message.msg_id)
        try:
            if message.kind == "group":
                await self._api.send_group_file(
                    message.target_id, file_info, "", message.msg_id, seq
                )
            else:
                await self._api.send_c2c_file(
                    message.target_id, file_info, "", message.msg_id, seq
                )
            log.info(
                "已发送文件 [%s] -> %s（%d 字节, seq=%d）",
                message.kind, message.target_id, size, seq,
            )
        except APIError as exc:
            if exc.code == ERR_DUPLICATE_MSG:
                log.warning(
                    "文件消息被去重 [%s] target=%s seq=%d",
                    message.kind, message.target_id, seq,
                )
                return
            log.error(
                "发送文件失败 [%s] target=%s status=%s code=%s body=%s",
                message.kind, message.target_id, exc.status, exc.code, exc.body[:300],
            )
        except Exception:
            log.exception(
                "发送文件失败 [%s] target=%s", message.kind, message.target_id
            )

    # 兼容旧调用名
    _send_reply = send_reply