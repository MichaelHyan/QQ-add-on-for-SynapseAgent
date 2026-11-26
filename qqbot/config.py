from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from typing import Any

import yaml

log = logging.getLogger(__name__)

DEFAULT_TOKEN_URLS = (
    "https://bots.qq.com/app/getAppAccessToken",
    "https://api.bot.qq.com/getAppAccessToken",
)

DEFAULT_API_BASES = (
    "https://api.sgroup.qq.com",
    "https://api.bot.qq.com",
)

INTENT_GROUP_AND_C2C = 1 << 25          # 群聊 @机器人 / 单聊消息
INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30  # 频道 @机器人

REPLY_MODE_FIXED = "fixed"
REPLY_MODE_AGENT = "agent"

# 启动时投递给后端的指令默认值：让 Agent 的工具层跳过 sender，
# 从而把 sender 调用原样回传给 QQ 端，由 QQ 端真正发文件。
DEFAULT_SKIPABLE_CMDS = ("#skipable sender",)


@dataclass
class Config:
    app_id: str = ""
    app_secret: str = ""
    intents: int = INTENT_GROUP_AND_C2C | INTENT_PUBLIC_GUILD_MESSAGES
    reply_text: str = "你好，我是 SynapseAgent（联调中）。收到你的消息啦～"
    shard: list[int] = field(default_factory=lambda: [0, 1])
    token_urls: list[str] = field(default_factory=lambda: list(DEFAULT_TOKEN_URLS))
    api_bases: list[str] = field(default_factory=lambda: list(DEFAULT_API_BASES))
    log_level: str = "INFO"
    log_dir: str = "logs"
    config_path: str = ""

    # ===== 后端 Agent =====
    # fixed: 固定回复；agent: 转发给后端 Agent
    reply_mode: str = REPLY_MODE_FIXED
    agent_endpoint: str = "http://127.0.0.1:56700/cnmd"
    agent_poll_interval: float = 3.0
    agent_timeout: float = 15.0
    # 单条消息从投递到彻底放弃的总上限（含分段生成）
    agent_pending_timeout: float = 300.0
    # 首次拿到内容的宽限期，超过则判定 Agent 无响应
    agent_first_reply_timeout: float = 60.0
    # 合并窗口（秒）：收到新内容就重新计时，空闲超过该时长才发出
    agent_merge_idle: float = 15.0
    # 群聊 @机器人 时是否加发言者前缀（仅群聊生效）
    agent_include_author: bool = True
    # 启动时写入后端系统提示词的文件（相对项目根目录），空串表示不初始化
    agent_prompt_file: str = "prompt.md"
    # 是否启用启动时的提示词注入（关闭后仍会投递 agent_skipable_cmds）
    agent_prompt_inject_enabled: bool = True
    # 等待后端确认提示词设置的最长时间（秒）
    agent_prompt_timeout: float = 90.0
    # 提示词注入完成后逐条投递的启动指令（如 #skipable sender）
    agent_skipable_cmds: list[str] = field(
        default_factory=lambda: list(DEFAULT_SKIPABLE_CMDS)
    )
    agent_queue_size: int = 32

    # ===== 媒体附件 =====
    # 是否把图片/音频/视频下载后 base64 编码交给 Agent
    agent_media_enabled: bool = True
    # 单个附件上限（字节），超过则降级为 URL
    agent_media_max_bytes: int = 2 * 1024 * 1024
    # 单条消息所有附件合计上限（字节）
    agent_media_total_bytes: int = 8 * 1024 * 1024
    # 单个附件下载超时（秒）
    agent_media_timeout: float = 20.0
    # base64 失败时是否改用 URL 形式交给 Agent
    agent_media_url_fallback: bool = True

    # ===== 文件收发 =====
    # 是否启用文件收发（收：落盘；发：上传并发送）
    agent_file_enabled: bool = True
    # 收到文件的保存目录（相对项目根目录时按项目根解析）
    agent_file_dir: str = "downloads"
    # 单个入站文件上限（字节），0 表示不限制（流式落盘）
    agent_file_max_bytes: int = 0
    # 单个出站文件上限（字节），0 表示不限制
    agent_file_send_max_bytes: int = 0
    # 文件下载超时（秒），大文件给足时间
    agent_file_timeout: float = 120.0

    @property
    def shard_tuple(self) -> tuple[int, int]:
        try:
            shard_id, shard_count = int(self.shard[0]), int(self.shard[1])
        except (IndexError, TypeError, ValueError):
            return (0, 1)
        return (shard_id, max(shard_count, 1))

    @property
    def is_agent_mode(self) -> bool:
        return str(self.reply_mode).strip().lower() == REPLY_MODE_AGENT

    @property
    def prompt_file_path(self) -> str:
        """提示词文件的绝对路径（相对路径按项目根目录解析）。"""
        name = (self.agent_prompt_file or "").strip()
        if not name:
            return ""
        if os.path.isabs(name):
            return name
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(root, name)

    @property
    def file_dir_path(self) -> str:
        """收文件目录的绝对路径（相对路径按项目根目录解析）。"""
        name = (self.agent_file_dir or "").strip()
        if not name:
            return ""
        if os.path.isabs(name):
            return os.path.normpath(name)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.normpath(os.path.join(root, name))

    def validate(self) -> None:
        if not self.app_id or not self.app_secret:
            raise ValueError(
                "缺少 app_id / app_secret。请复制 config.example.yaml 为 config.yaml 并填写，"
                "或设置环境变量 QQ_BOT_APP_ID / QQ_BOT_APP_SECRET。"
            )
        mode = str(self.reply_mode).strip().lower()
        if mode not in (REPLY_MODE_FIXED, REPLY_MODE_AGENT):
            raise ValueError(
                "reply_mode 只能是 '%s' 或 '%s'，当前为 %r"
                % (REPLY_MODE_FIXED, REPLY_MODE_AGENT, self.reply_mode)
            )
        if mode == REPLY_MODE_AGENT and not self.agent_endpoint:
            raise ValueError("reply_mode=agent 时必须配置 agent_endpoint")

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = path or os.path.join(project_root, "config.yaml")

        raw: dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fp:
                loaded = yaml.safe_load(fp) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"配置文件格式错误（应为映射结构）: {path}")
            raw = loaded
        else:
            log.warning("未找到配置文件 %s，将仅使用环境变量/默认配置", path)

        known = {f.name for f in fields(cls)} - {"config_path"}
        kwargs = {k: v for k, v in raw.items() if k in known and v is not None}

        # 环境变量优先级最高
        if os.getenv("QQ_BOT_APP_ID"):
            kwargs["app_id"] = os.environ["QQ_BOT_APP_ID"]
        if os.getenv("QQ_BOT_APP_SECRET"):
            kwargs["app_secret"] = os.environ["QQ_BOT_APP_SECRET"]
        if os.getenv("QQ_BOT_REPLY_TEXT"):
            kwargs["reply_text"] = os.environ["QQ_BOT_REPLY_TEXT"]
        if os.getenv("QQ_BOT_REPLY_MODE"):
            kwargs["reply_mode"] = os.environ["QQ_BOT_REPLY_MODE"]
        if os.getenv("QQ_BOT_AGENT_ENDPOINT"):
            kwargs["agent_endpoint"] = os.environ["QQ_BOT_AGENT_ENDPOINT"]
        if os.getenv("QQ_BOT_PROMPT_FILE"):
            kwargs["agent_prompt_file"] = os.environ["QQ_BOT_PROMPT_FILE"]
        if os.getenv("QQ_BOT_FILE_DIR"):
            kwargs["agent_file_dir"] = os.environ["QQ_BOT_FILE_DIR"]

        cfg = cls(**kwargs)
        cfg.config_path = os.path.abspath(path)

        try:
            cfg.intents = int(cfg.intents)
        except (TypeError, ValueError):
            cfg.intents = INTENT_GROUP_AND_C2C | INTENT_PUBLIC_GUILD_MESSAGES

        defaults = cls()
        for name in (
            "agent_poll_interval",
            "agent_timeout",
            "agent_pending_timeout",
            "agent_first_reply_timeout",
            "agent_merge_idle",
            "agent_prompt_timeout",
            "agent_media_timeout",
            "agent_file_timeout",
        ):
            try:
                setattr(cfg, name, float(getattr(cfg, name)))
            except (TypeError, ValueError):
                setattr(cfg, name, getattr(defaults, name))

        for name in (
            "agent_queue_size",
            "agent_media_max_bytes",
            "agent_media_total_bytes",
            "agent_file_max_bytes",
            "agent_file_send_max_bytes",
        ):
            try:
                setattr(cfg, name, max(0, int(getattr(cfg, name))))
            except (TypeError, ValueError):
                setattr(cfg, name, getattr(defaults, name))

        # 队列长度至少 1（其余允许 0 = 不限）
        if cfg.agent_queue_size < 1:
            cfg.agent_queue_size = defaults.agent_queue_size

        for name in (
            "agent_include_author",
            "agent_media_enabled",
            "agent_media_url_fallback",
            "agent_file_enabled",
            "agent_prompt_inject_enabled",
        ):
            setattr(
                cfg, name, _as_bool(getattr(cfg, name), getattr(defaults, name))
            )

        cfg.agent_prompt_file = str(cfg.agent_prompt_file or "").strip()
        cfg.agent_file_dir = str(cfg.agent_file_dir or "").strip()

        cfg.agent_skipable_cmds = _as_str_list(
            cfg.agent_skipable_cmds, list(DEFAULT_SKIPABLE_CMDS)
        )

        return cfg


def _as_str_list(value: Any, default: list[str]) -> list[str]:
    """把配置值规范成「非空字符串列表」。

    支持 YAML 列表、单个字符串（按行拆分）、None。
    """
    if value is None:
        return list(default)
    if isinstance(value, str):
        items: list[Any] = value.replace("\r", "").split("\n")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return list(default)

    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _as_bool(value: Any, default: bool) -> bool:
    """YAML 里可能写成 true/false、"true"/"false"、1/0。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default