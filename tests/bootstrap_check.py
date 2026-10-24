# 离线自测：验证提示词初始化流程
# 用法：python -m tests.bootstrap_check
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

from qqbot.bootstrap import (
    PROMPT_OVERWRITE,
    SET_PROMPT_PREFIX,
    bootstrap_prompt,
    load_prompt,
)


class FakeAgentClient:
    # 模拟后端：ask 记录内容，answer 按脚本返回

    def __init__(self, script):
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


def write_tmp(text):
    fd, path = tempfile.mkstemp(suffix=".md", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        fp.write(text)
    return path


def s1_empty_file_skipped():
    path = write_tmp("")
    client = FakeAgentClient([])
    ok = asyncio.run(bootstrap_prompt(client, path, timeout=5, poll_interval=0.1))
    assert ok is False, ok
    assert client.asked == [], client.asked
    os.remove(path)
    return "empty file skipped"


def s2_missing_file_skipped():
    client = FakeAgentClient([])
    ok = asyncio.run(
        bootstrap_prompt(client, "no_such_file.md", timeout=5, poll_interval=0.1)
    )
    assert ok is False, ok
    assert client.asked == [], client.asked
    return "missing file skipped"


def s3_payload_shape():
    path = write_tmp("你是突触助理。")
    client = FakeAgentClient([(0, "提示词已设置")])
    ok = asyncio.run(bootstrap_prompt(client, path, timeout=5, poll_interval=0.1))
    assert ok is True, ok
    assert len(client.asked) == 1, client.asked
    payload = client.asked[0]
    prefix = SET_PROMPT_PREFIX + PROMPT_OVERWRITE
    assert payload.startswith(prefix), payload[:60]
    assert payload.endswith("你是突触助理。"), payload[-30:]
    os.remove(path)
    return "payload shape"


def s4_drains_leftover():
    # 设置完成后再有残留响应，应被消费掉
    path = write_tmp("test prompt")
    client = FakeAgentClient([(0, "设置成功"), (0, "残留1"), (0, "残留2")])
    ok = asyncio.run(bootstrap_prompt(client, path, timeout=5, poll_interval=0.1))
    assert ok is True, ok
    assert client.answer_calls >= 2, client.answer_calls
    os.remove(path)
    return "drains leftover"


def s5_timeout_no_crash():
    # 后端一直返回 status=1 空内容，应超时退出且不抛异常
    path = write_tmp("prompt")
    client = FakeAgentClient([(1, "")] * 20)
    ok = asyncio.run(bootstrap_prompt(client, path, timeout=0.6, poll_interval=0.1))
    assert ok is False, ok
    os.remove(path)
    return "timeout no crash"


def s6_load_prompt():
    assert load_prompt("") == ""
    assert load_prompt("no_such.md") == ""
    path = write_tmp("abc")
    assert load_prompt(path) == "abc"
    os.remove(path)
    return "load prompt"


SCENARIOS = [
    s1_empty_file_skipped,
    s2_missing_file_skipped,
    s3_payload_shape,
    s4_drains_leftover,
    s5_timeout_no_crash,
    s6_load_prompt,
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
        print("BOOTSTRAP_CHECK_FAILED %d/%d" % (failed, len(SCENARIOS)))
        return 1
    print("BOOTSTRAP_CHECK_OK %d scenarios" % len(SCENARIOS))
    return 0


if __name__ == "__main__":
    sys.exit(main())