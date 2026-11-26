# 离线自测：文件收发（入站落盘 / 出站发送）
# 用法：python -m tests.files_check 或 python tests/files_check.py
from __future__ import annotations

import asyncio
import base64
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qqbot.files import (  # noqa: E402
    extract_file_tags,
    file_type_of,
    safe_name,
    save_incoming,
    to_base64,
    wrap_file,
)
from qqbot.handler import IncomingMessage, MessageHandler  # noqa: E402

# 标签统一由 files.wrap_file 生成，避免源码中出现完整标签字面
TAG_PREFIX = "agent_"


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
    def __init__(self, data=b"FILEDATA", status=200):
        self.data = data
        self.status = status
        self.requests = []

    def get(self, url, timeout=None):
        self.requests.append(url)
        return FakeResp(self.status, self.data)


class FakeAPI:
    """记录所有发送动作，不做真实网络请求。"""

    def __init__(self):
        self.texts = []
        self.uploads = []      # (kind, target, file_type, data_b64)
        self.file_msgs = []    # (kind, target, file_info, msg_seq)
        self.upload_names = []  # 上传时携带的 file_name
        self.upload_result = {"file_info": "FILEINFO-X"}

    async def send_group_message(self, target, content, msg_id=None, msg_seq=1):
        self.texts.append(content)
        return {}

    async def send_c2c_message(self, target, content, msg_id=None, msg_seq=1):
        self.texts.append(content)
        return {}

    async def send_channel_message(self, target, content, msg_id=None):
        self.texts.append(content)
        return {}

    async def send_dms_message(self, target, content, msg_id=None):
        self.texts.append(content)
        return {}

    async def upload_group_file(self, target, file_data, file_type=4,
                                srv_send_msg=False, file_name=""):
        self.uploads.append(("group", target, file_type, file_data))
        self.upload_names.append(file_name)
        return self.upload_result

    async def upload_c2c_file(self, target, file_data, file_type=4,
                              srv_send_msg=False, file_name=""):
        self.uploads.append(("c2c", target, file_type, file_data))
        self.upload_names.append(file_name)
        return self.upload_result

    async def send_group_file(self, target, file_info, content="",
                              msg_id=None, msg_seq=1):
        self.file_msgs.append(("group", target, file_info, msg_seq))
        return {}

    async def send_c2c_file(self, target, file_info, content="",
                            msg_id=None, msg_seq=1):
        self.file_msgs.append(("c2c", target, file_info, msg_seq))
        return {}


def make_msg(kind="group"):
    return IncomingMessage(
        kind=kind, event_type="GROUP_AT_MESSAGE_CREATE",
        content="", target_id="T1", msg_id="M1",
        author_id="A1", author_name="用户0001",
    )


def att(content_type="", url="http://x/a", filename=""):
    return {"content_type": content_type, "url": url, "filename": filename}


def mkfile(data=b"DATA", name="r.txt", suffix=""):
    tmp = tempfile.mkdtemp(suffix=suffix)
    path = os.path.join(tmp, name)
    with open(path, "wb") as fp:
        fp.write(data)
    return tmp, path


# ---------------- 场景 ----------------

def s1_extract_single():
    paths, plain = extract_file_tags("给你 " + wrap_file("D:/a/b.pdf"))
    assert paths == ["D:/a/b.pdf"], paths
    assert plain == "给你", plain
    return "extract single tag"


def s2_extract_multi():
    text = wrap_file("/x/1.txt") + "说明" + wrap_file("/x/2.zip")
    paths, plain = extract_file_tags(text)
    assert paths == ["/x/1.txt", "/x/2.zip"], paths
    assert plain == "说明", plain
    return "extract multiple tags"


def s3_extract_none():
    assert extract_file_tags("") == ([], ""), extract_file_tags("")
    assert extract_file_tags("纯文本") == ([], "纯文本")
    assert extract_file_tags(wrap_file("   ")) == ([], "")
    return "extract no tag"


def s4_file_type():
    assert file_type_of("a.jpg") == 1
    assert file_type_of("a.MP4") == 2
    assert file_type_of("a.amr") == 3
    assert file_type_of("a.pdf") == 4
    assert file_type_of("noext") == 4
    return "file type by extension"


def s5_to_base64():
    tmp, path = mkfile(b"HELLO")
    try:
        assert to_base64(path) == base64.b64encode(b"HELLO").decode("ascii")
        assert not to_base64(path).startswith("data:")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return "to_base64 plain"


def s6_save_incoming():
    tmp = tempfile.mkdtemp()
    try:
        first = save_incoming(b"ABC", "报告 1.txt", "MSG1234567890", tmp)
        assert first and os.path.isfile(first), first
        assert first.endswith(".txt"), first
        with open(first, "rb") as fp:
            assert fp.read() == b"ABC"
        assert "\\" not in first, first

        second = save_incoming(b"ABC", "报告 1.txt", "MSG1234567890", tmp)
        assert second and second != first, (first, second)
        assert len(os.listdir(tmp)) == 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return "save incoming unique"


def s7_safe_name():
    assert safe_name("a/b\\c.txt") == "c.txt", safe_name("a/b\\c.txt")
    assert safe_name("") == "unnamed"
    assert "/" not in safe_name("x/../y") and "\\" not in safe_name("x\\y")
    long_name = safe_name("z" * 300 + ".txt")
    assert len(long_name) <= 120, len(long_name)
    assert long_name.endswith(".txt"), long_name
    return "safe name"


def s8_send_text_then_file_group():
    tmp, path = mkfile(b"PAYLOAD")
    api = FakeAPI()
    handler = MessageHandler(
        api, file_enabled=True, file_dir=tmp, file_max_bytes=0,
    )
    try:
        asyncio.run(handler.send_reply(make_msg(), "报告好了\n" + wrap_file(path)))
        assert api.texts == ["报告好了"], api.texts
        assert len(api.uploads) == 1, api.uploads
        kind, target, ftype, data = api.uploads[0]
        assert (kind, target, ftype) == ("group", "T1", 4), api.uploads[0]
        assert data == base64.b64encode(b"PAYLOAD").decode("ascii")
        assert api.file_msgs == [("group", "T1", "FILEINFO-X", 2)], api.file_msgs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return "send text then file (group)"


def s9_send_text_then_file_c2c():
    tmp, path = mkfile(b"PAYLOAD", "a.png")
    api = FakeAPI()
    handler = MessageHandler(
        api, file_enabled=True, file_dir=tmp, file_max_bytes=0,
    )
    try:
        asyncio.run(handler.send_reply(make_msg("c2c"), "图片\n" + wrap_file(path)))
        assert api.texts == ["图片"], api.texts
        assert len(api.uploads) == 1, api.uploads
        kind, target, ftype, _ = api.uploads[0]
        assert (kind, target, ftype) == ("c2c", "T1", 1), api.uploads[0]
        assert api.file_msgs == [("c2c", "T1", "FILEINFO-X", 2)], api.file_msgs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return "send text then file (c2c)"


def s10_file_disabled_strips_tag():
    tmp, path = mkfile()
    api = FakeAPI()
    handler = MessageHandler(api, file_enabled=False, file_dir=tmp)
    try:
        asyncio.run(handler.send_reply(make_msg(), "结果在 " + wrap_file(path)))
        assert api.texts == ["结果在"], api.texts
        assert api.uploads == [] and api.file_msgs == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return "file disabled strips tag"


def s11_missing_file_silent():
    api = FakeAPI()
    handler = MessageHandler(api, file_enabled=True, file_dir="x")
    asyncio.run(handler.send_reply(make_msg(), wrap_file("/no/such.txt")))
    assert api.texts == [], api.texts
    assert api.uploads == [] and api.file_msgs == []
    return "missing file silent"


def s12_oversize_file_silent():
    tmp, path = mkfile(b"X" * 2000)
    api = FakeAPI()
    handler = MessageHandler(api, file_enabled=True, file_dir=tmp,
                             file_max_bytes=1000)
    try:
        asyncio.run(handler.send_reply(make_msg(), wrap_file(path)))
        assert api.texts == [], api.texts
        assert api.uploads == [] and api.file_msgs == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return "oversize file silent"


def s13_unlimited_size_passes():
    # file_max_bytes=0 表示不限，2000 字节也要能发出去
    tmp, path = mkfile(b"X" * 2000)
    api = FakeAPI()
    handler = MessageHandler(api, file_enabled=True, file_dir=tmp,
                             file_max_bytes=0)
    try:
        asyncio.run(handler.send_reply(make_msg(), wrap_file(path)))
        assert len(api.uploads) == 1, api.uploads
        assert len(api.file_msgs) == 1, api.file_msgs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return "unlimited size passes"


def s14_unsupported_kind_silent():
    tmp, path = mkfile()
    api = FakeAPI()
    handler = MessageHandler(api, file_enabled=True, file_dir=tmp,
                             file_max_bytes=0)
    try:
        asyncio.run(handler.send_reply(make_msg("guild"), wrap_file(path)))
        assert api.texts == [], api.texts
        assert api.uploads == [] and api.file_msgs == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return "guild kind silent skip"


def s15_upload_without_file_info_silent():
    tmp, path = mkfile()
    api = FakeAPI()
    api.upload_result = {}
    handler = MessageHandler(api, file_enabled=True, file_dir=tmp,
                             file_max_bytes=0)
    try:
        asyncio.run(handler.send_reply(make_msg(), wrap_file(path)))
        assert api.texts == [], api.texts
        assert api.file_msgs == [], api.file_msgs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return "upload without file_info silent"


def s16_c2c_incoming_file_saved():
    tmp = tempfile.mkdtemp()
    session = FakeSession(b"ZIPDATA")
    handler = MessageHandler(
        api=None, session=session, media_enabled=False,
        file_enabled=True, file_dir=tmp, file_max_bytes=0,
    )
    msg = make_msg("c2c")
    msg.content = "收一下"
    try:
        asyncio.run(handler._attach_media(
            msg, {"attachments": [att("file", "http://x/doc.zip", "doc.zip")]}
        ))
        assert msg.content.startswith("收一下\n[收到文件] doc.zip -> "), msg.content
        saved = msg.content.split(" -> ", 1)[1]
        assert os.path.isabs(saved) and os.path.isfile(saved), saved
        with open(saved, "rb") as fp:
            assert fp.read() == b"ZIPDATA"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return "c2c incoming file saved"


def s17_group_incoming_file_saved():
    # 需求是「收到文件保存在本地」，群聊与单聊一致处理
    tmp = tempfile.mkdtemp()
    session = FakeSession(b"ZIPDATA")
    handler = MessageHandler(
        api=None, session=session, media_enabled=False,
        file_enabled=True, file_dir=tmp, file_max_bytes=0,
    )
    msg = make_msg("group")
    msg.content = "收一下"
    try:
        asyncio.run(handler._attach_media(
            msg, {"attachments": [att("file", "http://x/doc.zip", "doc.zip")]}
        ))
        assert msg.content.startswith("收一下\n[收到文件] doc.zip -> "), msg.content
        saved = msg.content.split(" -> ", 1)[1]
        assert os.path.isabs(saved) and os.path.isfile(saved), saved
        with open(saved, "rb") as fp:
            assert fp.read() == b"ZIPDATA"
        assert len(os.listdir(tmp)) == 1, os.listdir(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return "group incoming file saved"


def s18_c2c_media_off_file_still_saved():
    tmp = tempfile.mkdtemp()
    session = FakeSession(b"DATA")
    handler = MessageHandler(
        api=None, session=session, media_enabled=False,
        file_enabled=True, file_dir=tmp, file_max_bytes=0,
    )
    msg = make_msg("c2c")
    msg.content = "看这个"
    try:
        asyncio.run(handler._attach_media(msg, {"attachments": [
            att("image/png", "http://x/a.png", "a.png"),
            att("file", "http://x/doc.zip", "doc.zip"),
        ]}))
        lines = msg.content.split("\n")
        assert lines[0] == "看这个", lines
        assert lines[1] == "[附件] a.png", lines
        assert lines[2].startswith("[收到文件] doc.zip -> "), lines
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return "c2c media off but file saved"


SCENARIOS = [
    s1_extract_single,
    s2_extract_multi,
    s3_extract_none,
    s4_file_type,
    s5_to_base64,
    s6_save_incoming,
    s7_safe_name,
    s8_send_text_then_file_group,
    s9_send_text_then_file_c2c,
    s10_file_disabled_strips_tag,
    s11_missing_file_silent,
    s12_oversize_file_silent,
    s13_unlimited_size_passes,
    s14_unsupported_kind_silent,
    s15_upload_without_file_info_silent,
    s16_c2c_incoming_file_saved,
    s17_group_incoming_file_saved,
    s18_c2c_media_off_file_still_saved,
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
        print("FILES_CHECK_FAILED %d/%d" % (failed, len(SCENARIOS)))
        return 1
    print("FILES_CHECK_OK %d scenarios" % len(SCENARIOS))
    return 0


if __name__ == "__main__":
    sys.exit(main())