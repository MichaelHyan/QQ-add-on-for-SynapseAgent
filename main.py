"""QQ-add-on-for-SynapseAgent 入口。

支持两种回复模式（config.yaml 的 reply_mode）：
  - fixed: 收到消息回固定文本，用于联调链路
  - agent: 把消息投递给后端 SynapseAgent，并周期轮询取回回复

agent 模式启动顺序（顺序不可颠倒）：
  1. bootstrap_prompt     把 prompt.md 写入后端系统提示词
  2. bootstrap_commands   投递启动指令（默认 ``#skipable sender``）
  3. bridge.start()       开始收发消息

第 2 步必须在第 1 步之后：提示词覆盖会重建后端的系统消息与命令表，
先发指令会被覆盖；且桥接的轮询协程会抢走 answer 响应，必须最后启动。

文件收发：
  - 收：附件流式落盘到 agent_file_dir，消息内容追加 ``[收到文件] 名 -> 路径``
  - 发：后端调用 ``sender <路径>`` 时因 ``#skipable sender`` 不会被执行，
    调用文本原样回传；QQ 端解析后上传并发给当前会话。
"""
from __future__ import annotations

import asyncio
import logging
import sys

import aiohttp

from qqbot.agent_client import AgentClient
from qqbot.api import QQBotAPI
from qqbot.auth import TokenManager
from qqbot.bootstrap import bootstrap_commands, bootstrap_prompt
from qqbot.bridge import AgentBridge
from qqbot.config import Config
from qqbot.gateway import GatewayClient
from qqbot.handler import FixedReplyBackend, MessageHandler
from qqbot.logger import setup_logging

log = logging.getLogger("main")

# 单实例锁：Windows 命名互斥体，进程退出时自动释放。
# 防止 start.bat 被重复启动，导致多条网关连接、同一事件被处理两次。
_INSTANCE_MUTEX = None

def _acquire_single_instance(name="QQ-addon-SynapseAgent-Mutex"):
    """返回 True 表示本进程取得单实例锁。"""
    global _INSTANCE_MUTEX
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = (
            wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.CreateMutexW(None, False, "Global" + chr(92) + name)
        if not handle:
            return True
        if ctypes.get_last_error() == 183:
            k32.CloseHandle(handle)
            return False
        _INSTANCE_MUTEX = handle
        return True
    except Exception:
        return True


def _mb(size: int) -> str:
    """字节数转人类可读，0 显示为「不限」。"""
    if not size:
        return "不限"
    return "%.1f MB" % (size / 1024 / 1024)


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
            file_enabled=cfg.agent_file_enabled,
            file_dir=cfg.file_dir_path,
            file_max_bytes=cfg.agent_file_max_bytes,
            file_send_max_bytes=cfg.agent_file_send_max_bytes,
            file_timeout=cfg.agent_file_timeout,
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

        if cfg.agent_file_enabled:
            log.info(
                "文件收发：启用（目录=%s 收件上限=%s 发件上限=%s 超时=%.0fs）",
                cfg.file_dir_path or "(未配置)",
                _mb(cfg.agent_file_max_bytes),
                _mb(cfg.agent_file_send_max_bytes),
                cfg.agent_file_timeout,
            )
        else:
            log.info("文件收发：关闭")

        bridge = None
        if cfg.is_agent_mode:
            client = AgentClient(
                endpoint=cfg.agent_endpoint,
                session=session,
                timeout=cfg.agent_timeout,
                poll_interval=cfg.agent_poll_interval,
            )

            # ① 先初始化提示词。桥接尚未启动，answer 不会被抢占。
            if cfg.agent_prompt_file and cfg.agent_prompt_inject_enabled:
                try:
                    await bootstrap_prompt(
                        client,
                        cfg.prompt_file_path,
                        timeout=cfg.agent_prompt_timeout,
                    )
                except Exception:
                    log.exception("提示词初始化异常，继续启动")
            else:
                log.info("提示词注入：已关闭（agent_prompt_inject_enabled=false）")

            # ② 再投递启动指令（#skipable sender 等）。
            #    必须晚于提示词覆盖，否则会被重建的命令表覆盖掉。
            if cfg.agent_skipable_cmds:
                try:
                    await bootstrap_commands(
                        client,
                        cfg.agent_skipable_cmds,
                        timeout=cfg.agent_prompt_timeout,
                    )
                except Exception:
                    log.exception("启动指令注入异常，继续启动")

            # ③ 最后启动桥接：它的轮询协程会消费 answer，
            #    早于初始化启动会与初始化争抢 msg_stack。
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
                "合并窗口=%.0fs 总超时=%.0fs 队列=%d 群聊前缀=%s 启动指令=%r）",
                cfg.agent_endpoint, cfg.agent_poll_interval,
                cfg.agent_first_reply_timeout, cfg.agent_merge_idle,
                cfg.agent_pending_timeout, cfg.agent_queue_size,
                "开" if cfg.agent_include_author else "关",
                cfg.agent_skipable_cmds,
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

    if not _acquire_single_instance():
        log.error("已有实例在运行，为避免重复连接，本次启动退出")
        return 3
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