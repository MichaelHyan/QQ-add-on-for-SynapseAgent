"""文件标签解析与落盘工具（收文件 / 发文件共用）。

标签名与 media.py 保持一致：用拼接生成，避免源码中出现完整标签字面。

收文件走「流式落盘」：边下边写，不把整份内容读进内存，
因此单个附件可以很大（上限由配置控制，0 表示不限）。
"""
from __future__ import annotations

import base64
import mimetypes
import os
import re
import time
from typing import Optional

# 标签名拼接生成，与 media.py 的 _TAG_PREFIX 约定一致
_TAG_PREFIX = "agent_"
FILE_TAG = _TAG_PREFIX + "file"

_OPEN = "<" + FILE_TAG + ">"
_CLOSE = "</" + FILE_TAG + ">"
_FILE_RE = re.compile(
    "<" + FILE_TAG + r"\s*>(.*?)</" + FILE_TAG + r"\s*>", re.DOTALL
)

# QQ 富媒体类型
FILE_TYPE_IMAGE = 1
FILE_TYPE_VIDEO = 2
FILE_TYPE_AUDIO = 3
FILE_TYPE_FILE = 4

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
_AUDIO_EXT = {".mp3", ".wav", ".ogg", ".amr", ".silk", ".m4a", ".flac"}

# 文件收发默认上限：0 表示不限制
DEFAULT_FILE_MAX_BYTES = 0
# 流式写盘的块大小
CHUNK_SIZE = 256 * 1024


def wrap_file(path: str) -> str:
    """把路径包成 <agent_file> 标签。"""
    return _OPEN + str(path) + _CLOSE


def extract_file_tags(text: str) -> tuple[list[str], str]:
    """提取文本中的 <agent_file> 路径。

    返回 (路径列表, 去掉标签并 strip 后的文本)。路径按出现顺序保留，重复不去重。
    """
    if not text:
        return [], ""
    paths: list[str] = []
    for match in _FILE_RE.finditer(text):
        raw = (match.group(1) or "").strip()
        if raw:
            paths.append(raw)
    return paths, _FILE_RE.sub("", text).strip()


def file_type_of(path: str) -> int:
    """按扩展名判断 QQ 富媒体类型。"""
    ext = os.path.splitext(str(path))[1].lower()
    if ext in _IMAGE_EXT:
        return FILE_TYPE_IMAGE
    if ext in _VIDEO_EXT:
        return FILE_TYPE_VIDEO
    if ext in _AUDIO_EXT:
        return FILE_TYPE_AUDIO
    return FILE_TYPE_FILE


def guess_mime(path: str) -> str:
    """猜 MIME，仅用于日志与调试。"""
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def to_base64(path: str) -> str:
    """把本地文件编码成 QQ 富媒体上传需要的 base64 字符串。

    平台 file_data 接受「在线链接」或「纯 base64」，这里给纯 base64，
    不带 ``data:`` 前缀——带前缀在部分接口会判为非法内容。

    注意：QQ 富媒体上传接口要求整份 file_data 在一个请求体里，
    所以这里无法流式，内存占用约为文件大小的 4/3。
    """
    with open(path, "rb") as fp:
        return base64.b64encode(fp.read()).decode("ascii")


def safe_name(name: str) -> str:
    """去掉路径分隔符与非法字符，保留扩展名，超长截断。"""
    name = os.path.basename(str(name or "").replace("\\", "/"))
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip(" .")
    if not name:
        name = "unnamed"
    if len(name) > 120:
        stem, ext = os.path.splitext(name)
        name = stem[: 120 - len(ext)] + ext
    return name


def allocate_target(filename: str, msg_id: str, base_dir: str) -> Optional[str]:
    """在 base_dir 下申请一个不冲突的落盘路径（不创建文件）。

    文件名格式：``时间戳_msgid前8_原文件名``，冲突时追加序号。
    返回绝对路径（正斜杠）；base_dir 为空返回 None。
    """
    if not base_dir:
        return None
    os.makedirs(base_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    mid = re.sub(r"[^0-9A-Za-z]", "", str(msg_id or ""))[:8] or "nomsg"
    target = os.path.join(
        base_dir, "%s_%s_%s" % (stamp, mid, safe_name(filename))
    )
    stem, ext = os.path.splitext(target)
    index = 1
    while os.path.exists(target):
        target = "%s_%d%s" % (stem, index, ext)
        index += 1
    return os.path.abspath(target).replace("\\", "/")


def save_incoming(
    data: bytes,
    filename: str,
    msg_id: str,
    base_dir: str,
) -> Optional[str]:
    """把入站附件的 bytes 写到固定目录，返回绝对路径（正斜杠）。失败返回 None。

    小文件可以用这个；大文件请用 :func:`download_to_file` 流式落盘。
    """
    if not data or not base_dir:
        return None
    target = allocate_target(filename, msg_id, base_dir)
    if not target:
        return None
    try:
        with open(target, "wb") as fp:
            fp.write(data)
        return target
    except OSError:
        return None


async def download_to_file(
    session,
    url: str,
    filename: str,
    msg_id: str,
    base_dir: str,
    max_bytes: int = DEFAULT_FILE_MAX_BYTES,
    timeout: float = 120.0,
    chunk_size: int = CHUNK_SIZE,
) -> tuple[Optional[str], int]:
    """流式下载附件并直接写盘，返回 ``(绝对路径, 字节数)``。

    - ``max_bytes <= 0`` 表示不限制体积（默认）
    - 边下边写，不把整份内容读进内存，几百 MB 也能落盘
    - 超限 / 下载失败 / 写入失败：清理半成品并返回 ``(None, 0)``
    """
    if not url or not base_dir:
        return None, 0

    target = allocate_target(filename, msg_id, base_dir)
    if not target:
        return None, 0

    limit = max(int(max_bytes), 0)
    written = 0
    try:
        async with session.get(url, timeout=max(float(timeout), 1.0)) as resp:
            if resp.status != 200:
                return None, 0

            declared = resp.headers.get("Content-Length")
            if limit and declared and str(declared).isdigit() and int(declared) > limit:
                return None, 0

            with open(target, "wb") as fp:
                async for chunk in resp.content.iter_chunked(chunk_size):
                    written += len(chunk)
                    if limit and written > limit:
                        fp.close()
                        _remove_quiet(target)
                        return None, 0
                    fp.write(chunk)

        if written == 0:
            _remove_quiet(target)
            return None, 0
        return target, written
    except Exception:
        _remove_quiet(target)
        return None, 0


def _remove_quiet(path: str) -> None:
    """删除文件，忽略一切异常（用于清理下载失败的半成品）。"""
    try:
        os.remove(path)
    except OSError:
        pass