from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)


class TokenError(RuntimeError):
    """获取 access_token 失败。"""


class TokenManager:
    """获取并自动刷新 QQ 机器人 access_token（有效期约 7200s）。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        token_urls: list[str],
        session: aiohttp.ClientSession,
        refresh_ahead: int = 60,
    ) -> None:
        self.app_id = app_id
        self._app_secret = app_secret
        self._urls = [u for u in token_urls if u]
        self._session = session
        self._refresh_ahead = refresh_ahead
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def token(self) -> Optional[str]:
        return self._token

    def invalidate(self) -> None:
        """标记当前 token 不可用，下次取用时强制刷新。"""
        self._token = None
        self._expires_at = 0.0

    async def get_token(self, force: bool = False) -> str:
        async with self._lock:
            expired = time.time() >= self._expires_at - self._refresh_ahead
            if not force and self._token and not expired:
                return self._token
            await self._fetch()
            if not self._token:
                raise TokenError("access_token 为空")
            return self._token

    async def _fetch(self) -> None:
        payload = {"appId": self.app_id, "clientSecret": self._app_secret}
        errors: list[str] = []

        for url in self._urls:
            try:
                async with self._session.post(url, json=payload, timeout=15) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        errors.append(f"{url} -> HTTP {resp.status}: {text[:200]}")
                        continue
                    data = json.loads(text)
            except Exception as exc:  # 网络异常则换下一个地址
                errors.append(f"{url} -> {type(exc).__name__}: {exc}")
                continue

            body = data.get("data") if isinstance(data.get("data"), dict) else data
            token = body.get("access_token") or data.get("access_token")
            if not token:
                errors.append(f"{url} -> 响应缺少 access_token: {str(data)[:200]}")
                continue

            try:
                expires_in = int(float(body.get("expires_in") or data.get("expires_in") or 7200))
            except (TypeError, ValueError):
                expires_in = 7200

            self._token = str(token)
            self._expires_at = time.time() + expires_in
            log.info("获取 access_token 成功（有效期 %ss，来源 %s）", expires_in, url)
            return

        self._token = None
        self._expires_at = 0.0
        raise TokenError("获取 access_token 失败：" + " | ".join(errors))