# 离线自测：媒体附件下载与编码
# 用法：python -m tests.media_check
from __future__ import annotations

import asyncio
import base64
import sys

from qqbot.handler import MessageHandler
from qqbot.media import build_media_parts, detect_tag, excerpt_media


class FakeContent:
    def __init__(self, data):
        self._data = data

    async def iter_chunked(self, n):
        for i in range(0, len(self._data), n):
            yield self._data[i:i + n]


class FakeResp:
    def __init__(self, status, data, headers=None):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    # 按 URL 返回不同内容；URL 里含 fail 则 404
    def __init__(self, data=b"\x89PNG-fake-bytes", status=200, headers=None):
        self.data = data
        self.status = status
        self.headers = headers or {}
        self.requests = []

    def get(self, url, timeout=None):
        self.requests.append(url)
        if "fail" in url:
            return FakeResp(404, b"", self.headers)
        return FakeResp(self.status, self.data, self.headers)


def att(content_type="", url="http://x/a", filename="", **extra):
    item = {"content_type": content_type, "url": url, "filename": filename}
    item.update(extra)
    return item


# ---------------- 场景 ----------------

def s1_detect_by_content_type():
    assert detect_tag("image/png") == "image"
    assert detect_tag("image/jpeg") == "image"
    assert detect_tag("video/mp4") == "video"
    assert detect_tag("audio/wav") == "audio"
    assert detect_tag("voice") == "audio"
    assert detect_tag("image") == "image"
    assert detect_tag("file") is None
    assert detect_tag("") is None
    return "detect by content_type"


def s2_detect_by_extension():
    assert detect_tag("", "a.jpg") == "image"
    assert detect_tag("", "", "http://x/b.MP4?sign=1") == "video"
    assert detect_tag("", "c.amr") == "audio"
    assert detect_tag("", "d.zip") is None
    return "detect by extension"


def s3_image_embedded():
    session = FakeSession(b"IMGBYTES")
    parts = asyncio.run(
        build_media_parts(
            session, [att("image/png", "http://x/a.png")],
            max_bytes=1024, total_bytes=4096, timeout=5,
        )
    )
    assert len(parts) == 1, parts
    expect = "<image>%s</image>" % base64.b64encode(b"IMGBYTES").decode("ascii")
    assert parts[0] == expect, parts[0]
    return "image embedded"


def s4_audio_and_video_tags():
    session = FakeSession(b"DATA")
    parts = asyncio.run(
        build_media_parts(
            session,
            [att("audio/wav", "http://x/a.wav"), att("video/mp4", "http://x/v.mp4")],
            max_bytes=1024, total_bytes=4096, timeout=5,
        )
    )
    assert len(parts) == 2, parts
    b64 = base64.b64encode(b"DATA").decode("ascii")
    assert parts[0] == "<audio>%s</audio>" % b64, parts[0]
    assert parts[1] == "<video>%s</video>" % b64, parts[1]
    return "audio and video tags"


def s5_oversize_falls_back_to_url():
    session = FakeSession(b"X" * 5000)
    parts = asyncio.run(
        build_media_parts(
            session, [att("image/png", "http://x/big.png")],
            max_bytes=100, total_bytes=4096, timeout=5, url_fallback=True,
        )
    )
    assert parts == ["<image>http://x/big.png</image>"], parts
    return "oversize fallback to url"


def s6_oversize_without_fallback():
    session = FakeSession(b"X" * 5000)
    parts = asyncio.run(
        build_media_parts(
            session, [att("image/png", "http://x/big.png")],
            max_bytes=100, total_bytes=4096, timeout=5, url_fallback=False,
        )
    )
    assert parts == ["<image></image>"], parts
    return "oversize without fallback"


def s7_download_failure():
    session = FakeSession(b"")
    parts = asyncio.run(
        build_media_parts(
            session, [att("image/png", "http://x/fail.png")],
            max_bytes=1024, total_bytes=4096, timeout=5, url_fallback=True,
        )
    )
    assert parts == ["<image>http://x/fail.png</image>"], parts
    return "download failure fallback"


def s8_non_media_attachment():
    session = FakeSession(b"DATA")
    parts = asyncio.run(
        build_media_parts(
            session, [att("file", "http://x/doc.zip", "doc.zip")],
            max_bytes=1024, total_bytes=4096, timeout=5,
        )
    )
    assert parts == ["[附件] doc.zip"], parts
    assert session.requests == [], session.requests
    return "non media attachment"


def s9_total_budget():
    # 第一个占满预算，第二个应降级为 URL
    session = FakeSession(b"Y" * 300)
    parts = asyncio.run(
        build_media_parts(
            session,
            [att("image/png", "http://x/a.png"), att("image/png", "http://x/b.png")],
            max_bytes=1000, total_bytes=300, timeout=5, url_fallback=True,
        )
    )
    assert len(parts) == 2, parts
    assert parts[0].startswith("<image>") and "base64" not in parts[0]
    assert parts[1] == "<image>http://x/b.png</image>", parts[1]
    return "total budget"


def s10_no_session_or_no_attachments():
    assert asyncio.run(
        build_media_parts(None, [att("image/png")], max_bytes=1, total_bytes=1, timeout=1)
    ) == []
    session = FakeSession(b"DATA")
    assert asyncio.run(
        build_media_parts(session, [], max_bytes=1, total_bytes=1, timeout=1)
    ) == []
    assert asyncio.run(
        build_media_parts(session, [None, "x", 1], max_bytes=999, total_bytes=999, timeout=1)
    ) == []
    return "no session or no attachments"


def s11_handler_attaches_media():
    # handler 层：附件应追加到消息内容
    session = FakeSession(b"IMG")
    handler = MessageHandler(
        api=None, session=session,
        media_enabled=True, media_max_bytes=1024, media_total_bytes=4096,
    )
    message = __import__("qqbot.handler", fromlist=["IncomingMessage"]).IncomingMessage(
        kind="group", event_type="GROUP_AT_MESSAGE_CREATE",
        content="看这个", target_id="G1", msg_id="M1",
    )
    data = {"attachments": [att("image/png", "http://x/a.png")]}
    asyncio.run(handler._attach_media(message, data))
    b64 = base64.b64encode(b"IMG").decode("ascii")
    assert message.content == "看这个\n<image>%s</image>" % b64, message.content
    return "handler attaches media"


def s12_handler_media_disabled():
    session = FakeSession(b"IMG")
    handler = MessageHandler(api=None, session=session, media_enabled=False)
    from qqbot.handler import IncomingMessage
    message = IncomingMessage(
        kind="group", event_type="GROUP_AT_MESSAGE_CREATE",
        content="看这个", target_id="G1", msg_id="M1",
    )
    asyncio.run(handler._attach_media(message, {"attachments": [att("image/png")]}))
    assert message.content == "看这个", message.content
    assert session.requests == [], session.requests
    return "handler media disabled"


def s13_excerpt_media():
    text = "<image>%s</image>你好" % ("A" * 500)
    out = excerpt_media(text)
    assert "<image>(500 字符)</image>" in out, out
    assert len(out) <= 200, len(out)
    assert excerpt_media("") == ""
    assert excerpt_media("纯文本") == "纯文本"
    return "excerpt media"


SCENARIOS = [
    s1_detect_by_content_type,
    s2_detect_by_extension,
    s3_image_embedded,
    s4_audio_and_video_tags,
    s5_oversize_falls_back_to_url,
    s6_oversize_without_fallback,
    s7_download_failure,
    s8_non_media_attachment,
    s9_total_budget,
    s10_no_session_or_no_attachments,
    s11_handler_attaches_media,
    s12_handler_media_disabled,
    s13_excerpt_media,
]


def main():
    failed = 0
    for fn in SCENARIOS:
        try:
            desc = fn()
        except AssertionError as exc:
            failed += 1
            print("FAIL %s: %s" % (fn.__name__, exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL %s: %s: %s" % (fn.__name__, type(exc).__name__, exc))
        else:
            print("PASS %s (%s)" % (fn.__name__, desc))

    if failed:
        print("MEDIA_CHECK_FAILED %d/%d" % (failed, len(SCENARIOS)))
        return 1
    print("MEDIA_CHECK_OK %d scenarios" % len(SCENARIOS))
    return 0


if __name__ == "__main__":
    sys.exit(main())