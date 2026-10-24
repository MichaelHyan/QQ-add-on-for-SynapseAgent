"""离线自测：不联网，用假 API 验证事件解析与回复分发逻辑。

用法：python -m tests.offline_check
"""
from __future__ import annotations

import asyncio
import sys

from qqbot.handler import (
    FixedReplyBackend,
    IncomingMessage,
    MessageHandler,
    clean_content,
)


class FakeAPI:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def send_group_message(self, group_openid, content, msg_id=None, msg_seq=1):
        self.calls.append(("group", group_openid, content, msg_id, msg_seq))
        return {}

    async def send_c2c_message(self, user_openid, content, msg_id=None, msg_seq=1):
        self.calls.append(("c2c", user_openid, content, msg_id, msg_seq))
        return {}

    async def send_channel_message(self, channel_id, content, msg_id=None):
        self.calls.append(("guild", channel_id, content, msg_id))
        return {}

    async def send_dms_message(self, guild_id, content, msg_id=None):
        self.calls.append(("dm", guild_id, content, msg_id))
        return {}


GROUP_EVENT = {
    "id": "MSG_GROUP_1",
    "content": " 你好呀 ",
    "group_openid": "GROUP_OPENID_1",
    "author": {"member_openid": "MEMBER_1"},
}

C2C_EVENT = {
    "id": "MSG_C2C_1",
    "content": "单聊测试",
    "author": {"user_openid": "USER_OPENID_1"},
}

GUILD_EVENT = {
    "id": "MSG_GUILD_1",
    "content": "<@!123456> 频道测试",
    "channel_id": "CHANNEL_1",
    "author": {"id": "USER_GUILD_1"},
}

DM_EVENT = {
    "id": "MSG_DM_1",
    "content": "私信测试",
    "guild_id": "GUILD_1",
    "author": {"id": "USER_DM_1"},
}


async def main() -> int:
    api = FakeAPI()
    handler = MessageHandler(api, FixedReplyBackend("固定回复"))

    await handler.on_event("GROUP_AT_MESSAGE_CREATE", GROUP_EVENT)
    await handler.on_event("C2C_MESSAGE_CREATE", C2C_EVENT)
    await handler.on_event("AT_MESSAGE_CREATE", GUILD_EVENT)
    await handler.on_event("DIRECT_MESSAGE_CREATE", DM_EVENT)
    # 通知类事件与未支持事件应被忽略且不报错
    await handler.on_event("GROUP_ADD_ROBOT", {})
    await handler.on_event("SOME_UNKNOWN_EVENT", {})

    expected = [
        ("group", "GROUP_OPENID_1", "固定回复", "MSG_GROUP_1", 1),
        ("c2c", "USER_OPENID_1", "固定回复", "MSG_C2C_1", 1),
        ("guild", "CHANNEL_1", "固定回复", "MSG_GUILD_1"),
        ("dm", "GUILD_1", "固定回复", "MSG_DM_1"),
    ]
    assert api.calls == expected, f"实际: {api.calls}"

    # 同一 msg_id 多次回复时 msg_seq 需递增
    await handler.on_event("GROUP_AT_MESSAGE_CREATE", GROUP_EVENT)
    assert api.calls[-1][-1] == 2, f"msg_seq 未递增: {api.calls[-1]}"

    # @ 标记清理
    assert clean_content("<@!123456>  你好 ") == "你好"
    assert clean_content("") == ""
    assert clean_content("纯文本") == "纯文本"

    # session_key 稳定
    msg = IncomingMessage("group", "t", "c", "G", "M", "A")
    assert msg.session_key == "group:G:A"

    # 超长内容截断
    long_text = "啊" * 3000
    await handler._send_reply(
        IncomingMessage("group", "t", "c", "G", "M_LONG"), long_text
    )
    assert len(api.calls[-1][2]) <= 1800, len(api.calls[-1][2])
    assert api.calls[-1][2].endswith("...")

    print("OFFLINE_CHECK_OK", len(api.calls), "calls")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))