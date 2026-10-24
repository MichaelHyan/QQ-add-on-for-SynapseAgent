# 离线自测：不联网，用假 Agent 验证桥接链路
# 用法：python -m tests.agent_bridge_check
#
# 设计说明：
#  - 每个场景独立 asyncio.run，避免跨场景状态污染（生产是单进程单 bridge）
#  - 等待用 asyncio.Event + wait_for，不依赖轮询次数
from __future__ import annotations

import asyncio
import sys

from qqbot.bridge import AgentBridge, split_text, strip_tool_calls
from qqbot.handler import IncomingMessage, make_display_name

# 标签用拼接构造，避免源码出现完整的工具调用标签字面量
TAG_OPEN = "<" + "tool_call" + ">"
TAG_CLOSE = "</" + "tool_call" + ">"


def wrap_tag(body):
    return TAG_OPEN + body + TAG_CLOSE


class FakeAgentClient:
    # 模拟 AgentClient：answer 按预设脚本依次返回

    def __init__(self, script, poll_interval=0.02):
        self.poll_interval = poll_interval
        self.endpoint = "fake://cnmd"
        self.script = list(script)
        self.asked = []
        self.answer_calls = 0

    async def ask(self, content):
        self.asked.append(content)

    async def answer(self):
        self.answer_calls += 1
        if self.script:
            return self.script.pop(0)
        return 0, ""


def msg(content, author_name="", target="U1", msg_id="M1", kind="c2c"):
    # 不同 kind 对应的事件类型，便于构造真实场景
    event_type = {
        "group": "GROUP_AT_MESSAGE_CREATE",
        "c2c": "C2C_MESSAGE_CREATE",
        "guild": "AT_MESSAGE_CREATE",
        "dm": "DIRECT_MESSAGE_CREATE",
    }.get(kind, "C2C_MESSAGE_CREATE")
    return IncomingMessage(
        kind=kind,
        event_type=event_type,
        content=content,
        target_id=target,
        msg_id=msg_id,
        author_name=author_name,
    )


async def run_scenario(script, message, *, timeout=30.0, first_to=60.0,
                       merge_idle=15.0, max_wait=20.0, settle=0.3,
                       poll_interval=0.02, include_author=True):
    """投递一条消息，等首条回复后再等 settle 秒收集其余。"""
    sent = []
    first = asyncio.Event()

    async def send_reply(message, text):
        sent.append(
            {"target": message.target_id, "kind": message.kind, "text": text}
        )
        first.set()

    client = FakeAgentClient(script, poll_interval=poll_interval)
    bridge = AgentBridge(
        client, send_reply,
        max_queue=4, pending_timeout=timeout,
        first_reply_timeout=first_to, merge_idle=merge_idle,
        include_author=include_author,
    )
    await bridge.start()
    try:
        bridge.submit(message)
        try:
            await asyncio.wait_for(first.wait(), timeout=max_wait)
            await asyncio.sleep(settle)
        except asyncio.TimeoutError:
            pass
    finally:
        await bridge.stop()
    return sent, client


def _texts(sent):
    return [item["text"] for item in sent]


# ---------------- 场景 ----------------

def s1_single_segment():
    sent, client = asyncio.run(run_scenario([(0, "第一段回复")], msg("你好")))
    assert client.asked == ["你好"], client.asked
    assert _texts(sent) == ["第一段回复"], sent
    return "single segment"


def s2_merged_with_newline():
    # 窗口内多段应合并成一条，用换行连接
    sent, _ = asyncio.run(
        run_scenario(
            [(1, "部分A"), (1, "部分B"), (0, "结尾C")], msg("讲个故事"),
            merge_idle=15.0,
        )
    )
    assert len(sent) == 1, "expect merged into 1, got %d: %s" % (len(sent), sent)
    assert sent[0]["text"] == "部分A\n部分B\n结尾C", repr(sent[0]["text"])
    return "merged with newline"


def s3_window_expiry_flushes():
    # status 一直为 1 且无新内容：空闲超过窗口应先把已有内容发出
    script = [(1, "先发这段")] + [(1, "")] * 400
    sent, _ = asyncio.run(
        run_scenario(
            script, msg("慢任务"), merge_idle=0.5, max_wait=5.0, settle=0.2
        )
    )
    assert len(sent) >= 1, "window expiry should flush, got %s" % sent
    assert sent[0]["text"] == "先发这段", sent[0]["text"]
    return "window expiry flushes"


def s4_empty_then_content():
    # status=0 空内容（Agent 尚未产出）应继续等待，不该提前收工
    sent, client = asyncio.run(
        run_scenario(
            [(0, ""), (0, ""), (0, ""), (0, "终于来了")], msg("在吗"),
            first_to=10.0,
        )
    )
    assert _texts(sent) == ["终于来了"], sent
    assert client.answer_calls >= 4, client.answer_calls
    return "empty status=0 keeps waiting"


def s5_first_reply_timeout():
    sent, client = asyncio.run(
        run_scenario([], msg("死机了吗"), first_to=1.0)
    )
    assert len(sent) == 1, "expect 1 hint, got %d: %s" % (len(sent), sent)
    assert "未返回内容" in sent[0]["text"], sent[0]["text"]
    assert client.answer_calls >= 2, client.answer_calls
    return "first reply timeout"


def s6_single_long_segment_split():
    # 单次返回超长内容，应切分且不超过条数上限
    sent, _ = asyncio.run(
        run_scenario([(0, "啊" * 4000)], msg("写长文"), first_to=10.0)
    )
    assert 1 <= len(sent) <= 4, "segment count out of range: %d" % len(sent)
    assert all(len(item["text"]) <= 1800 for item in sent), [
        len(item["text"]) for item in sent
    ]
    return "single long segment split"


def s7_overflow_notice():
    # 条数上限仍受控，且超限时不崩
    script = [(1, "第%d段" % i) for i in range(1, 8)] + [(0, "")]
    sent, _ = asyncio.run(
        run_scenario(script, msg("一直发"), first_to=10.0, settle=0.4)
    )
    assert 1 <= len(sent) <= 4, "segment count out of range: %d" % len(sent)
    return "overflow capped at 4"


def s8_tool_call_stripped():
    raw = wrap_tag("send_file a.txt") + "真正的回复"
    sent, _ = asyncio.run(run_scenario([(0, raw)], msg("发文件")))
    assert _texts(sent) == ["真正的回复"], sent
    return "tool call stripped"


def s9_empty_input():
    sent, client = asyncio.run(run_scenario([], msg("   ")))
    assert client.asked == [], client.asked
    assert len(sent) == 1 and "请输入" in sent[0]["text"], sent
    return "empty input guarded"


def s10_group_prefix_on():
    # 群聊 + 开关打开：应加前缀
    sent, client = asyncio.run(
        run_scenario(
            [(0, "我是助手")],
            msg("你是谁", author_name="小明", kind="group", target="G1"),
        )
    )
    assert client.asked == ["小明：你是谁"], client.asked
    assert _texts(sent) == ["我是助手"], sent
    return "group prefix on"


def s11_group_prefix_off():
    # 群聊 + 开关关闭：不加前缀
    sent, client = asyncio.run(
        run_scenario(
            [(0, "好的")],
            msg("你是谁", author_name="小明", kind="group", target="G1"),
            include_author=False,
        )
    )
    assert client.asked == ["你是谁"], client.asked
    return "group prefix off"


def s12_c2c_never_prefix():
    # 单聊：即使开关打开，也不加前缀
    sent, client = asyncio.run(
        run_scenario(
            [(0, "好的")],
            msg("你是谁", author_name="小明", kind="c2c", target="U9"),
        )
    )
    assert client.asked == ["你是谁"], client.asked
    return "c2c never prefixed"


def s13_guild_never_prefix():
    # 频道：即使开关打开，也不加前缀
    sent, client = asyncio.run(
        run_scenario(
            [(0, "好的")],
            msg("你是谁", author_name="真昵称", kind="guild", target="C1"),
        )
    )
    assert client.asked == ["你是谁"], client.asked
    return "guild never prefixed"


def s14_dm_never_prefix():
    # 频道私信：即使开关打开，也不加前缀
    sent, client = asyncio.run(
        run_scenario(
            [(0, "好的")],
            msg("你是谁", author_name="真昵称", kind="dm", target="G2"),
        )
    )
    assert client.asked == ["你是谁"], client.asked
    return "dm never prefixed"


def s15_author_name_fallback():
    # 群聊无昵称时用 openid 短哈希，且同一 openid 稳定
    a = make_display_name("", "openid-abc-123")
    b = make_display_name("", "openid-abc-123")
    c = make_display_name("", "openid-xyz-999")
    assert a == b, (a, b)
    assert a != c, (a, c)
    assert a.startswith("用户"), a
    assert make_display_name("真昵称", "openid-abc-123") == "真昵称"
    assert make_display_name("", "") == "未知用户"
    return "author name fallback"


def s16_pure_functions():
    assert strip_tool_calls(wrap_tag("x") + "ok") == "ok"
    assert strip_tool_calls("") == ""
    assert strip_tool_calls("纯文本") == "纯文本"
    assert split_text("") == []
    assert split_text("短文本") == ["短文本"]
    assert len(split_text("a" * 5000)) >= 2
    return "pure functions"


SCENARIOS = [
    s1_single_segment,
    s2_merged_with_newline,
    s3_window_expiry_flushes,
    s4_empty_then_content,
    s5_first_reply_timeout,
    s6_single_long_segment_split,
    s7_overflow_notice,
    s8_tool_call_stripped,
    s9_empty_input,
    s10_group_prefix_on,
    s11_group_prefix_off,
    s12_c2c_never_prefix,
    s13_guild_never_prefix,
    s14_dm_never_prefix,
    s15_author_name_fallback,
    s16_pure_functions,
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
        print("AGENT_BRIDGE_CHECK_FAILED %d/%d" % (failed, len(SCENARIOS)))
        return 1
    print("AGENT_BRIDGE_CHECK_OK %d scenarios" % len(SCENARIOS))
    return 0


if __name__ == "__main__":
    sys.exit(main())