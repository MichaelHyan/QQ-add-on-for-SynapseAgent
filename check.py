"""自检脚本：仅验证凭证与网关地址是否可用，不建立长连接。

用法：python check.py
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

from qqbot.api import QQBotAPI
from qqbot.auth import TokenManager
from qqbot.config import Config
from qqbot.logger import setup_logging

log = logging.getLogger("check")


async def run(cfg: Config) -> int:
    async with aiohttp.ClientSession() as session:
        tm = TokenManager(cfg.app_id, cfg.app_secret, cfg.token_urls, session)
        token = await tm.get_token()
        log.info("[OK] access_token 获取成功：%s...（长度 %s）", token[:8], len(token))

        api = QQBotAPI(tm, cfg.api_bases, session)
        gateway = await api.get_gateway()
        log.info("[OK] 网关地址：%s", gateway.get("url"))
        log.info("     建议分片数：%s", gateway.get("shards"))
        log.info("     会话限制：%s", gateway.get("session_start_limit"))
    return 0


def main() -> int:
    cfg = Config.load()
    setup_logging(cfg.log_level, cfg.log_dir)
    try:
        cfg.validate()
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    log.info("配置：app_id=%s intents=%s", cfg.app_id, cfg.intents)
    try:
        return asyncio.run(run(cfg))
    except Exception as exc:
        log.error("[FAIL] 自检失败：%s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())