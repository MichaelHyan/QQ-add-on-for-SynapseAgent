"""QQ-add-on-for-SynapseAgent 入口。

支持两种回复模式（config.yaml 的 reply_mode）：
  - fixed: 收到消息回固定文本，用于联调链路
  - agent: 把消息投递给后端 SynapseAgent，并周期轮询取回回复

agent 模式下，启动时会先把 prompt.md 的内容写入后端系统提示词。
图片/音频/视频附件会下载并 base64 编码后交给后端多模态解析。
"""
from __future__ import annotations

import asyncio
import logging
import sys

import aiohttp

from qqbot.agent_client import AgentClient
from qqbot.api import QQBotAPI
from qqbot.auth import TokenManager
from qqbot.bootstrap import bootstrap_prompt
from qqbot.bridge import AgentBridge
from qqbot.config import Config
from qqbot.gateway import GatewayClient
from qqbot.handler import FixedReplyBackend, MessageHandler
from qqbot.logger import setup_logging

log = logging.getLogger("main")


async def run(cfg: Config) -> None:
    async with aiohttp.ClientSession() as session:
        token_manager = TokenManager(
            cfg.app_id, cfg.app_secret, cfg.token_urls, session
        )
        # 启动前先验证凭证，避免带着错误配置进入重连循环
        await token_manager.get_token()

        api = QQBotAPI(token_manager, cfg.api_bases, session)
        handler = MessageHandler(
            api,
            session=session,
            media_enabled=cfg.agent_media_enabled,
            media_max_bytes=cfg.agent_media_max_bytes,
            media_total_bytes=cfg.agent_media_total_bytes,
            media_timeout=cfg.agent_media_timeout,
            media_url_fallback=cfg.agent_media_url_fallback,
        )

        if cfg.agent_media_enabled:
            log.info(
                "媒体处理：启用（单附件上限 %.1f MB，总量上限 %.1f MB，超时 %.0fs）",
                cfg.agent_media_max_bytes / 1024 / 1024,
                cfg.agent_media_total_bytes / 1024 / 1024,
                cfg.agent_media_timeout,
            )
        else:
            log.info("媒体处理：关闭")

        bridge = None
        if cfg.is_agent_mode:
            client = AgentClient(
                endpoint=cfg.agent_endpoint,
                session=session,
                timeout=cfg.agent_timeout,
                poll_interval=cfg.agent_poll_interval,
            )

            # 先初始化提示词，再启动桥接。
            # 顺序很重要：桥接的轮询协程会抢走 answer 的响应，
            # 若先启动会与初始化争抢 msg_stack。
            if cfg.agent_prompt_file:
                try:
                    await bootstrap_prompt(
                        client,
                        cfg.prompt_file_path,
                        timeout=cfg.agent_prompt_timeout,
                    )
                except Exception:
                    log.exception("提示词初始化异常，继续启动")

            bridge = AgentBridge(
                client=client,
                send_reply=handler.send_reply,
                max_queue=cfg.agent_queue_size,
                pending_timeout=cfg.agent_pending_timeout,
                first_reply_timeout=cfg.agent_first_reply_timeout,
                merge_idle=cfg.agent_merge_idle,
                include_author=cfg.agent_include_author,
            )
            handler.set_message_callback(bridge.submit)
            await bridge.start()
            log.info(
                "回复模式：Agent（endpoint=%s 轮询=%.1fs 首响应宽限=%.0fs "
                "合并窗口=%.0fs 总超时=%.0fs 队列=%d 群聊前缀=%s）",
                cfg.agent_endpoint, cfg.agent_poll_interval,
                cfg.agent_first_reply_timeout, cfg.agent_merge_idle,
                cfg.agent_pending_timeout, cfg.agent_queue_size,
                "开" if cfg.agent_include_author else "关",
            )
        else:
            handler.set_backend(FixedReplyBackend(cfg.reply_text))
            log.info("回复模式：固定回复（内容：%r）", cfg.reply_text)

        gateway = GatewayClient(
            api=api,
            token_manager=token_manager,
            intents=cfg.intents,
            on_event=handler.on_event,
            session=session,
            shard=cfg.shard_tuple,
        )

        try:
            await gateway.run()
        finally:
            if bridge is not None:
                await bridge.stop()
            await gateway.close()


def main() -> int:
    cfg = Config.load()
    setup_logging(cfg.log_level, cfg.log_dir)
    try:
        cfg.validate()
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    log.info("配置文件: %s", cfg.config_path)
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        log.info("收到中断信号，已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())