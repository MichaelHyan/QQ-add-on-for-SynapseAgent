from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import aiohttp

from .auth import TokenManager

log = logging.getLogger(__name__)


class APIError(RuntimeError):
    """OpenAPI 调用失败。"""

    def __init__(self, status: int, body: str, path: str = "") -> None:
        self.status = status
        self.body = body
        self.path = path
        super().__init__(f"[{status}] {path} -> {body[:300]}")

    @property
    def code(self) -> Optional[int]:
        try:
            data = json.loads(self.body)
        except (TypeError, ValueError):
            return None
        return data.get("code") if isinstance(data, dict) else None


class QQBotAPI:
    """QQ 机器人 OpenAPI 轻量封装。"""

    def __init__(
        self,
        token_manager: TokenManager,
        api_bases: list[str],
        session: aiohttp.ClientSession,
        timeout: int = 20,
    ) -> None:
        self._tm = token_manager
        self._bases = [b.rstrip("/") for b in api_bases if b]
        if not self._bases:
            raise ValueError("api_bases 不能为空")
        self._preferred = 0
        self._session = session
        self._timeout = timeout

    # ---------------- 基础请求 ----------------

    def _ordered_bases(self) -> list[str]:
        return self._bases[self._preferred:] + self._bases[: self._preferred]

    def _remember_base(self, base: str) -> None:
        while self._bases and self._bases[self._preferred] != base:
            self._preferred = (self._preferred + 1) % len(self._bases)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        last_error: Optional[Exception] = None

        for attempt in (0, 1):
            token = await self._tm.get_token(force=(attempt == 1))
            headers = {
                "Authorization": f"QQBot {token}",
                "Content-Type": "application/json",
                "User-Agent": "SynapseAgent-QQBot/0.1.0",
            }
            retry_after_refresh = False
            for base in self._ordered_bases():
                url = f"{base}{path}"
                try:
                    async with self._session.request(
                        method,
                        url,
                        headers=headers,
                        json=json_body,
                        params=params,
                        timeout=self._timeout,
                    ) as resp:
                        text = await resp.text()
                        if resp.status == 401 and attempt == 0:
                            log.warning("access_token 被拒绝，刷新后重试")
                            self._tm.invalidate()
                            last_error = APIError(401, text, path)
                            retry_after_refresh = True
                            break
                        if resp.status >= 400:
                            raise APIError(resp.status, text, path)
                        self._remember_base(base)
                        return json.loads(text) if text else {}
                except (aiohttp.ClientConnectorError, aiohttp.ClientOSError,
                        aiohttp.ServerDisconnectedError, asyncio.TimeoutError) as exc:
                    last_error = exc
                    log.warning("请求 %s 失败（%s），尝试下一个地址", url, type(exc).__name__)
                    continue
            if not retry_after_refresh:
                break

        if isinstance(last_error, Exception):
            raise last_error
        raise APIError(0, "未知错误", path)

    # ---------------- 网关 ----------------

    async def get_gateway(self) -> dict:
        errors: list[str] = []
        for path in ("/gateway/bot", "/gateway"):
            try:
                return await self.request("GET", path)
            except APIError as exc:
                errors.append(f"{path}: {exc}")
                continue
        raise RuntimeError("获取 WebSocket 网关地址失败: " + " | ".join(errors))

    # ---------------- 消息发送 ----------------

    async def send_group_message(
        self,
        group_openid: str,
        content: str,
        msg_id: Optional[str] = None,
        msg_seq: int = 1,
    ) -> dict:
        body: dict[str, Any] = {"content": content, "msg_type": 0}
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = msg_seq
        return await self.request(
            "POST", f"/v2/groups/{group_openid}/messages", json_body=body
        )

    async def send_c2c_message(
        self,
        user_openid: str,
        content: str,
        msg_id: Optional[str] = None,
        msg_seq: int = 1,
    ) -> dict:
        body: dict[str, Any] = {"content": content, "msg_type": 0}
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = msg_seq
        return await self.request(
            "POST", f"/v2/users/{user_openid}/messages", json_body=body
        )

    async def send_channel_message(
        self,
        channel_id: str,
        content: str,
        msg_id: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {"content": content}
        if msg_id:
            body["msg_id"] = msg_id
        return await self.request(
            "POST", f"/channels/{channel_id}/messages", json_body=body
        )

    async def send_dms_message(
        self,
        guild_id: str,
        content: str,
        msg_id: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {"content": content}
        if msg_id:
            body["msg_id"] = msg_id
        return await self.request("POST", f"/dms/{guild_id}/messages", json_body=body)