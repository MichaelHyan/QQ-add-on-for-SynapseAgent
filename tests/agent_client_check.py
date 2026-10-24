# 离线自测：验证 AgentClient 对真实响应结构的解析
# 用法：python -m tests.agent_client_check
#
# 重点回归：后端响应是嵌套结构 {"ok":true,"result":{"status":..,"response":..}}
# 早期实现从顶层读 status/response，导致永远拿到 0 和空串。
from __future__ import annotations

import asyncio
import json
import sys

from qqbot.agent_client import AgentClient, AgentError


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status = status_code
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return json.dumps(self._payload, ensure_ascii=False)

    async def json(self, content_type=None):
        return self._payload


class FakeSession:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self._status_code = status_code
        self.requests = []

    def post(self, url, json=None, timeout=None):
        self.requests.append({"url": url, "body": json})
        return FakeResponse(self._status_code, self._payload)


def make_client(payload, status_code=200):
    session = FakeSession(payload, status_code)
    client = AgentClient(
        endpoint="http://fake/cnmd",
        session=session,
        timeout=5.0,
        poll_interval=0.01,
    )
    return client, session


def s1_nested_processing():
    """status=1 + result 嵌套：应正确读到 1。"""
    payload = {"ok": True, "result": {"status": 1, "response": ""}}
    client, _ = make_client(payload)
    status, response = asyncio.run(client.answer())
    assert status == 1, status
    assert response == "", repr(response)
    return "nested status=1"


def s2_nested_with_content():
    """status=0 + result 嵌套且带内容：应读到内容和 0。"""
    payload = {"ok": True, "result": {"status": 0, "response": "你好"}}
    client, _ = make_client(payload)
    status, response = asyncio.run(client.answer())
    assert status == 0, status
    assert response == "你好", repr(response)
    return "nested status=0 with content"


def s3_nested_empty_done():
    """status=0 + result 嵌套且空内容：保持 0 和空串。"""
    payload = {"ok": True, "result": {"status": 0, "response": ""}}
    client, _ = make_client(payload)
    status, response = asyncio.run(client.answer())
    assert status == 0, status
    assert response == "", repr(response)
    return "nested empty"


def s4_flat_fallback():
    """兼容简化实现：业务字段直接在顶层。"""
    payload = {"ok": True, "status": 1, "response": "扁平"}
    client, _ = make_client(payload)
    status, response = asyncio.run(client.answer())
    assert status == 1, status
    assert response == "扁平", repr(response)
    return "flat fallback"


def s5_ask_body_shape():
    """ask 请求体结构必须是 {"method":"ask","params":{"content":..}}。"""
    payload = {"ok": True, "result": {"status": 1, "response": ""}}
    client, session = make_client(payload)
    asyncio.run(client.ask("测试内容"))
    body = session.requests[-1]["body"]
    assert body == {"method": "ask", "params": {"content": "测试内容"}}, body
    return "ask request body"


def s6_answer_body_shape():
    """answer 请求体结构必须是 {"method":"answer","params":{"type":"rp"}}。"""
    payload = {"ok": True, "result": {"status": 0, "response": ""}}
    client, session = make_client(payload)
    asyncio.run(client.answer())
    body = session.requests[-1]["body"]
    assert body == {"method": "answer", "params": {"type": "rp"}}, body
    return "answer request body"


def s7_ok_false_raises():
    """ok=false 应抛 AgentError。"""
    payload = {"ok": False, "error": {"message": "boom"}}
    client, _ = make_client(payload)
    try:
        asyncio.run(client.answer())
    except AgentError:
        return "ok=false raises"
    raise AssertionError("ok=false 未抛异常")


def s8_non_string_response():
    """response 非字符串时应转成字符串，不崩。"""
    payload = {"ok": True, "result": {"status": 0, "response": 123}}
    client, _ = make_client(payload)
    status, response = asyncio.run(client.answer())
    assert status == 0, status
    assert response == "123", repr(response)
    return "non-string response"


SCENARIOS = [
    s1_nested_processing,
    s2_nested_with_content,
    s3_nested_empty_done,
    s4_flat_fallback,
    s5_ask_body_shape,
    s6_answer_body_shape,
    s7_ok_false_raises,
    s8_non_string_response,
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
        print("AGENT_CLIENT_CHECK_FAILED %d/%d" % (failed, len(SCENARIOS)))
        return 1
    print("AGENT_CLIENT_CHECK_OK %d scenarios" % len(SCENARIOS))
    return 0


if __name__ == "__main__":
    sys.exit(main())