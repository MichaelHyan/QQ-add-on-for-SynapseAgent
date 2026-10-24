from __future__ import annotations

import asyncio
import logging
from typing import Any, Tuple

import aiohttp

log = logging.getLogger(__name__)


class AgentError(RuntimeError):
    """调用后端 Agent 失败。"""


class AgentClient:
    """SynapseAgent 本地 HTTP 客户端。

    协议与 wechat-add-on 一致（周期性 POST 轮询）：

        POST {endpoint}  {"method": "ask",    "params": {"content": "..."}}
        POST {endpoint}  {"method": "answer", "params": {"type": "rp"}}
        POST {endpoint}  {"method": "ping"}

    响应结构（注意是嵌套的）：

        {"ok": true, "result": {"status": 1, "response": ""}}

    status / response 在 result 子对象里，不在顶层。
    """

    def __init__(
        self,
        endpoint: str,
        session: aiohttp.ClientSession,
        timeout: float = 15.0,
        poll_interval: float = 3.0,
        answer_type: str = "rp",
    ) -> None:
        self._endpoint = endpoint
        self._session = session
        self._timeout = max(float(timeout), 1.0)
        self.poll_interval = max(float(poll_interval), 0.2)
        self._answer_type = answer_type

    @property
    def endpoint(self) -> str:
        return self._endpoint

    async def _call(self, method: str, **params: Any) -> dict:
        body = {"method": method, "params": params}
        try:
            async with self._session.post(
                self._endpoint, json=body, timeout=self._timeout
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise AgentError(f"HTTP {resp.status}: {text[:200]}")
                data = await resp.json(content_type=None)
        except AgentError:
            raise
        except asyncio.TimeoutError as exc:
            raise AgentError("请求超时") from exc
        except aiohttp.ClientError as exc:
            raise AgentError(f"{type(exc).__name__}: {exc}") from exc
        except ValueError as exc:
            raise AgentError(f"响应不是合法 JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise AgentError(f"响应格式异常: {str(data)[:200]}")
        if data.get("ok") is False:
            raise AgentError(f"Agent 返回失败: {str(data)[:200]}")
        return data

    @staticmethod
    def _extract_result(data: dict) -> dict:
        """取业务字段所在的字典。

        标准响应把业务字段放在 result 子对象里；
        为兼容简化实现，result 缺失时回退到顶层。
        """
        result = data.get("result")
        if isinstance(result, dict):
            return result
        return data

    async def ping(self) -> bool:
        try:
            await self._call("ping")
            return True
        except AgentError as exc:
            log.debug("ping 失败: %s", exc)
            return False

    async def ask(self, content: str) -> None:
        await self._call("ask", content=content)

    async def answer(self) -> Tuple[int, str]:
        """返回 (status, response)。

        status: 1=仍在生成 / 0=本轮结束（需结合 response 是否为空判断）
        """
        data = await self._call("answer", type=self._answer_type)
        body = self._extract_result(data)

        try:
            status = int(body.get("status") or 0)
        except (TypeError, ValueError):
            status = 0

        response = body.get("response")
        if response is None:
            response = ""
        elif not isinstance(response, str):
            response = str(response)

        return status, response