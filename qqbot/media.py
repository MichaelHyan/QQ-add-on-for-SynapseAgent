from __future__ import annotations

import base64
import logging
import os
import re
from typing import Any, Iterable, Optional

import aiohttp

log = logging.getLogger(__name__)

# content_type 精确匹配
_MEDIA_EXACT = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "voice": "audio",
}

# content_type 前缀匹配
_MEDIA_PREFIX = (
    ("image/", "image"),
    ("video/", "video"),
    ("audio/", "audio"),
)

# 扩展名兜底（部分事件不给 content_type）
_MEDIA_EXT = {
    ".jpg": "image", ".jpeg": "image", ".png": "image",
    ".gif": "image", ".webp": "image", ".bmp": "image",
    ".mp4": "video", ".mov": "video", ".mkv": "video", ".webm": "video",
    ".mp3": "audio", ".wav": "audio", ".ogg": "audio",
    ".amr": "audio", ".silk": "audio", ".m4a": "audio", ".flac": "audio",
}

# 用于日志摘要，把长 base64 折叠掉
_TAG_BODY_RE = re.compile(r"<(image|video|audio)>([^<]*)</\1>", re.DOTALL)


def detect_tag(
    content_type: str = "", filename: str = "", url: str = ""
) -> Optional[str]:
    """判断附件属于 image / video / audio，无法识别返回 None。"""
    ct = (content_type or "").strip().lower()
    if ct:
        if ct in _MEDIA_EXACT:
            return _MEDIA_EXACT[ct]
        for prefix, tag in _MEDIA_PREFIX:
            if ct.startswith(prefix):
                return tag

    for name in (filename, url):
        if not name:
            continue
        stem = str(name).split("?")[0]
        ext = os.path.splitext(stem)[1].lower()
        if ext in _MEDIA_EXT:
            return _MEDIA_EXT[ext]
    return None


def excerpt_media(text: str, limit: int = 200) -> str:
    """日志用摘要：把标签里的 base64 折叠成长度说明。"""
    if not text:
        return ""

    def _repl(match: re.Match) -> str:
        tag = match.group(1)
        return "<%s>(%d 字符)</%s>" % (tag, len(match.group(2)), tag)

    return _TAG_BODY_RE.sub(_repl, text)[:limit]


async def fetch_base64(
    session: aiohttp.ClientSession,
    url: str,
    max_bytes: int,
    timeout: float,
) -> Optional[tuple]:
    """下载附件并 base64 编码。

    返回 (base64 字符串, 原始字节数)；超限或失败返回 None。
    流式读取，一旦超过上限立即中止，避免把大文件读进内存。
    """
    if not url:
        return None

    limit = max(int(max_bytes), 1)
    buf = bytearray()

    try:
        client_timeout = aiohttp.ClientTimeout(total=max(float(timeout), 1.0))
        async with session.get(url, timeout=client_timeout) as resp:
            if resp.status != 200:
                log.warning("附件下载失败 HTTP %s：%s", resp.status, url[:120])
                return None

            declared = resp.headers.get("Content-Length")
            if declared and str(declared).isdigit() and int(declared) > limit:
                log.warning("附件声明大小 %s 超过上限 %s，跳过", declared, limit)
                return None

            async for chunk in resp.content.iter_chunked(64 * 1024):
                buf.extend(chunk)
                if len(buf) > limit:
                    log.warning("附件实际大小超过上限 %s 字节，跳过", limit)
                    return None
    except Exception as exc:
        log.warning("附件下载异常：%s: %s", type(exc).__name__, exc)
        return None

    if not buf:
        log.info("附件内容为空，跳过：%s", url[:120])
        return None

    return base64.b64encode(bytes(buf)).decode("ascii"), len(buf)


async def build_media_parts(
    session: Optional[aiohttp.ClientSession],
    attachments: Iterable[Any],
    *,
    max_bytes: int,
    total_bytes: int,
    timeout: float,
    url_fallback: bool = True,
) -> list:
    """把附件列表转换成待拼接的文本片段。

    规则：
      - 可识别的媒体 → ``<image>base64</image>`` 形式
      - 非媒体附件   → ``[附件] 文件名``
      - 超限/失败    → 降级为 ``<image>URL</image>`` 或失败提示
    """
    if session is None:
        return []

    parts: list = []
    used = 0
    total_limit = max(int(total_bytes), 1)

    for att in attachments or []:
        if not isinstance(att, dict):
            continue

        content_type = att.get("content_type") or att.get("contentType") or ""
        url = att.get("url") or ""
        filename = att.get("filename") or att.get("file_name") or ""

        tag = detect_tag(content_type, filename, url)

        # 不是媒体：只留一个可读标记，避免用户以为消息丢了
        if tag is None:
            label = filename or url
            if label:
                parts.append("[附件] %s" % label)
            continue

        # 总量已用完：剩余附件降级
        if used >= total_limit:
            log.warning("附件总量已达上限 %s 字节，后续降级", total_limit)
            if url_fallback and url:
                parts.append("<%s>%s</%s>" % (tag, url, tag))
            continue

        remaining = total_limit - used
        fetched = await fetch_base64(
            session, url, min(max_bytes, remaining), timeout
        )

        if fetched is None:
            if url_fallback and url:
                parts.append("<%s>%s</%s>" % (tag, url, tag))
            else:
                parts.append("<%s></%s>" % (tag, tag))
            continue

        encoded, size = fetched
        used += size
        log.info("已嵌入 %s 附件：%d 字节（base64 %d 字符）", tag, size, len(encoded))
        parts.append("<%s>%s</%s>" % (tag, encoded, tag))

    return parts