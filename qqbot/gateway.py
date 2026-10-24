from __future__ import annotations

import asyncio
import json
import logging
import platform
from typing import Awaitable, Callable, Optional

import aiohttp

from .api import QQBotAPI
from .auth import TokenManager

log = logging.getLogger(__name__)

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11
OP_CALLBACK_ACK = 12

EventCallback = Callable[[str, dict], Awaitable[None]]


class _Reconnect(Exception):
    """内部信号：需要关闭当前连接并重连。"""


class GatewayClient:
    """QQ 机器人 WebSocket 网关客户端（鉴权 / 心跳 / 重连 / 事件分发）。"""

    def __init__(
        self,
        api: QQBotAPI,
        token_manager: TokenManager,
        intents: int,
        on_event: EventCallback,
        session: aiohttp.ClientSession,
        shard: tuple[int, int] = (0, 1),
    ) -> None:
        self._api = api
        self._tm = token_manager
        self._intents = intents
        self._on_event = on_event
        self._session = session
        self._shard = shard

        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._seq: Optional[int] = None
        self._session_id: Optional[str] = None
        self._heartbeat_interval: float = 41.25
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._ack_pending = False
        self._closed = False

    # ---------------- 生命周期 ----------------

    async def run(self) -> None:
        backoff = 1
        while not self._closed:
            try:
                await self._connect_once()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except _Reconnect:
                log.info("准备重连网关")
            except Exception as exc:
                log.exception("网关连接异常: %s", exc)

            if self._closed:
                break
            log.info("%s 秒后尝试重连", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def close(self) -> None:
        self._closed = True
        await self._stop_heartbeat()
        if self._ws and not self._ws.closed:
            await self._ws.close()

    # ---------------- 连接 ----------------

    async def _connect_once(self) -> None:
        gateway = await self._fetch_gateway()
        log.info("连接网关: %s", gateway)

        try:
            self._ws = await self._session.ws_connect(
                gateway, heartbeat=None, max_msg_size=0, timeout=20
            )
        except Exception as exc:
            raise _Reconnect(f"建立 WebSocket 失败: {exc}") from exc

        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except ValueError:
                        log.warning("收到无法解析的数据帧")
                        continue
                    await self._on_payload(payload)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log.warning("WebSocket 错误: %s", self._ws.exception())
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    break
        finally:
            await self._stop_heartbeat()
            if self._ws and not self._ws.closed:
                await self._ws.close()
            self._ws = None
            log.info("网关连接已断开")

    async def _fetch_gateway(self) -> str:
        data = await self._api.get_gateway()
        url = data.get("url")
        if not url:
            raise RuntimeError(f"网关响应异常: {data}")
        total_shards = data.get("shards")
        if total_shards:
            log.info(
                "网关建议分片数: %s（当前连接分片 %s/%s）",
                total_shards, self._shard[0], self._shard[1],
            )
        return url

    # ---------------- 收包处理 ----------------

    async def _on_payload(self, payload: dict) -> None:
        op = payload.get("op")
        seq = payload.get("s")
        if seq:
            self._seq = seq

        if op == OP_HELLO:
            interval_ms = (payload.get("d") or {}).get("heartbeat_interval", 41250)
            self._heartbeat_interval = max(float(interval_ms) / 1000.0, 1.0)
            log.info("收到 Hello，心跳间隔 %ss", self._heartbeat_interval)
            self._start_heartbeat()
            await self._authenticate()
            return

        if op == OP_HEARTBEAT_ACK:
            self._ack_pending = False
            log.debug("心跳 ACK，seq=%s", self._seq)
            return

        if op == OP_HEARTBEAT:
            await self._send({"op": OP_HEARTBEAT, "d": self._seq})
            return

        if op == OP_RECONNECT:
            log.info("服务端要求重连")
            raise _Reconnect()

        if op == OP_INVALID_SESSION:
            log.warning("会话失效，清除登录态并重新鉴权")
            self._session_id = None
            self._seq = None
            raise _Reconnect()

        if op == OP_CALLBACK_ACK:
            return

        if op == OP_DISPATCH:
            event_type = payload.get("t") or ""
            data = payload.get("d") or {}
            await self._dispatch(event_type, data)
            return

        log.debug("未处理的 op=%s payload=%s", op, str(payload)[:200])

    async def _dispatch(self, event_type: str, data: dict) -> None:
        if event_type == "READY":
            self._session_id = data.get("session_id")
            user = data.get("user") or {}
            log.info(
                "鉴权成功：session_id=%s 机器人=%s(%s) shard=%s",
                self._session_id, user.get("username"), user.get("id"),
                data.get("shard"),
            )
            return

        if event_type == "RESUMED":
            log.info("会话恢复成功，继续接收事件")
            return

        if not event_type:
            return

        # 不阻塞收包循环
        asyncio.create_task(self._safe_event(event_type, data))

    async def _safe_event(self, event_type: str, data: dict) -> None:
        try:
            await self._on_event(event_type, data)
        except Exception:
            log.exception("处理事件 %s 时出错", event_type)

    # ---------------- 鉴权 / 心跳 ----------------

    async def _authenticate(self) -> None:
        token = await self._tm.get_token()
        if self._session_id and self._seq is not None:
            log.info("发送 Resume（session_id=%s seq=%s）", self._session_id, self._seq)
            await self._send({
                "op": OP_RESUME,
                "d": {
                    "token": f"QQBot {token}",
                    "session_id": self._session_id,
                    "seq": self._seq,
                },
            })
        else:
            os_name = platform.system().lower() or "linux"
            await self._send({
                "op": OP_IDENTIFY,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": self._intents,
                    "shard": [self._shard[0], self._shard[1]],
                    "properties": {
                        "$os": os_name,
                        "$browser": "synapse-qq-addon",
                        "$device": "synapse-qq-addon",
                    },
                },
            })
            log.info("已发送 Identify（intents=%s）", self._intents)

    def _start_heartbeat(self) -> None:
        self._stop_heartbeat_sync()
        self._ack_pending = False
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def _stop_heartbeat_sync(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = None

    async def _stop_heartbeat(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                if self._ack_pending:
                    log.warning("上次心跳未收到 ACK，主动断开重连")
                    if self._ws and not self._ws.closed:
                        await self._ws.close()
                    return
                self._ack_pending = True
                await self._send({"op": OP_HEARTBEAT, "d": self._seq})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("心跳循环异常退出")

    async def _send(self, payload: dict) -> None:
        if not self._ws or self._ws.closed:
            return
        try:
            await self._ws.send_str(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            log.warning("发送数据帧失败: %s", exc)