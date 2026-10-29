from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path

from .agent_client import AgentClient, AgentError

log = logging.getLogger(__name__)

# 后端 set_prompt 只有识别到这个前缀才会「直接覆盖」提示词，
# 否则会把参数当成 prompt_loader 的文件名去加载。
PROMPT_OVERWRITE = "PROMPTOVERWRITE"
SET_PROMPT_PREFIX = "#bot prompt "

# prompt.md 中用于占位「项目基准目录」的标记
BASE_PATH_PLACEHOLDER = "###BASEPATH###"


def normalize_base_path(raw: str) -> str:
    """把配置里的基准路径规范化为「使用 / 分割的绝对路径」。

    - 展开 ``~`` 与环境变量
    - 相对路径以项目根目录（本文件上两级，即 addon/）为基准
    - Windows 反斜杠转换为正斜杠
    - 无法解析时返回空串
    """
    if not raw:
        return ""
    text = str(raw).strip().strip('"').strip("'")
    if not text:
        return ""
    text = os.path.expanduser(os.path.expandvars(text))
    try:
        path = Path(text)
        if not path.is_absolute():
            root = Path(__file__).resolve().parent.parent
            path = root / path
        return path.resolve().as_posix()
    except (OSError, ValueError) as exc:
        log.warning("BASE_PATH 规范化失败（%s）：%s", raw, exc)
        return ""


def _config_candidates(prompt_path: str) -> list[str]:
    """按优先级列出可能的 config.yaml 路径。

    本项目 config.yaml 与 prompt.md 同在 addon/ 根目录。
    """
    here = Path(__file__).resolve().parent
    candidates: list[Path] = []
    if prompt_path:
        candidates.append(Path(prompt_path).expanduser().resolve().parent / "config.yaml")
    candidates.extend([
        here.parent / "config.yaml",          # addon/config.yaml（本项目实际位置）
        here / "config.yaml",                 # addon/qqbot/config.yaml
        here.parent.parent / "config.yaml",
        Path.cwd() / "config.yaml",
    ])
    result: list[str] = []
    for item in candidates:
        key = str(item)
        if key not in result:
            result.append(key)
    return result


def _read_base_path_from_config(config_path: str) -> str:
    """从单个 config.yaml 中读取 BASE_PATH 原始值。"""
    # 优先用 PyYAML（若项目已安装），否则回退到逐行解析
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None

    if yaml is not None:
        try:
            with open(config_path, "r", encoding="utf-8") as fp:
                data = yaml.safe_load(fp)
            if isinstance(data, dict):
                raw = data.get("BASE_PATH") or data.get("base_path")
                if raw:
                    return str(raw)
        except Exception as exc:  # noqa: BLE001
            log.debug("解析配置文件失败，回退文本解析：%s", exc)

    try:
        with open(config_path, "r", encoding="utf-8") as fp:
            for line in fp:
                match = re.match(r"^\s*BASE_PATH\s*:\s*(.+?)\s*(?:#.*)?$", line)
                if match:
                    value = match.group(1).strip().strip('"').strip("'")
                    if value:
                        return value
    except OSError as exc:
        log.debug("读取配置文件失败（%s）：%s", config_path, exc)
    return ""


def load_base_path(prompt_path: str) -> str:
    """从 config.yaml 中读取 BASE_PATH（原始值）。找不到返回空串。"""
    for config_path in _config_candidates(prompt_path):
        if not os.path.isfile(config_path):
            continue
        raw = _read_base_path_from_config(config_path)
        if raw:
            log.info("从配置文件读取 BASE_PATH：%s（%s）", raw, config_path)
            return raw
    log.info("未在配置文件中找到 BASE_PATH")
    return ""


def apply_base_path(text: str, base_path: str) -> str:
    """把 prompt 文本里的 ###BASEPATH### 替换为规范化后的基准路径。"""
    if BASE_PATH_PLACEHOLDER not in text:
        return text
    normalized = normalize_base_path(base_path)
    if not normalized:
        log.warning(
            "prompt 中使用了 %s，但未配置可用的 BASE_PATH，保持原样",
            BASE_PATH_PLACEHOLDER,
        )
        return text
    log.info("替换 %s -> %s", BASE_PATH_PLACEHOLDER, normalized)
    return text.replace(BASE_PATH_PLACEHOLDER, normalized)


def load_prompt(path: str, base_path: str = "") -> str:
    """读取提示词文件；不存在或为空返回空串。

    读取完成后会把 ###BASEPATH### 替换为 base_path 的绝对路径。
    """
    if not path:
        return ""
    if not os.path.exists(path):
        log.info("提示词文件不存在：%s", path)
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fp:
            text = fp.read()
    except OSError as exc:
        log.warning("读取提示词文件失败：%s", exc)
        return ""
    return apply_base_path(text, base_path)


async def _drain(client: AgentClient, rounds: int = 6) -> int:
    """把 msg_stack 里残留的响应消费掉，避免污染第一条用户消息。"""
    drained = 0
    for _ in range(rounds):
        try:
            status, response = await client.answer()
        except AgentError:
            break
        if not response:
            if status == 0:
                break
            continue
        drained += 1
    return drained


async def bootstrap_prompt(
    client: AgentClient,
    prompt_path: str,
    timeout: float = 90.0,
    poll_interval: float = 1.0,
    base_path: str | None = None,
) -> bool:
    """启动时把 prompt.md 的内容写入后端 Agent 的系统提示词。

    流程：
      1. 读取 prompt.md，为空则跳过（允许先留空，之后再补）
      2. 将 ###BASEPATH### 替换为 config.yaml 中 BASE_PATH 的绝对路径
         （base_path 为 None 时自动从配置文件查找）
      3. 以 ``#bot prompt PROMPTOVERWRITE<内容>`` 投递给后端
      4. 轮询 answer，等后端处理完（status 回 0）
      5. 把残留响应消费干净，防止混入第一条用户回复

    返回是否成功投递并收到回应。失败不抛异常，只记日志——
    初始化失败不应该阻止机器人上线。
    """
    if base_path is None:
        base_path = load_base_path(prompt_path)

    text = load_prompt(prompt_path, base_path)
    if not text.strip():
        log.info("提示词文件为空，跳过初始化（%s）", prompt_path or "(未配置)")
        return False

    payload = f"{SET_PROMPT_PREFIX}{PROMPT_OVERWRITE}{text}"
    log.info(
        "正在初始化提示词：%s（%d 字符，投递 %d 字符）",
        prompt_path, len(text), len(payload),
    )

    try:
        await client.ask(payload)
    except AgentError as exc:
        log.warning("提示词投递失败：%s", exc)
        return False

    deadline = time.monotonic() + max(float(timeout), 1.0)
    got_response = False
    last_status = None

    while time.monotonic() < deadline:
        await asyncio.sleep(max(float(poll_interval), 0.2))
        try:
            status, response = await client.answer()
        except AgentError as exc:
            log.debug("初始化期间轮询失败：%s", exc)
            continue

        last_status = status
        if response:
            got_response = True
            log.info("初始化回应：%s", response[:120])
            continue

        # 无内容且后端空闲：说明设置已完成
        if status == 0:
            break

    leftover = await _drain(client)
    if leftover:
        log.info("清理了 %d 条初始化残留响应", leftover)

    if got_response:
        log.info("提示词初始化完成")
    elif last_status == 0:
        log.info("提示词初始化完成（后端未回显）")
    else:
        log.warning("提示词初始化超时（%.0fs），后端可能仍在处理", timeout)
        return False

    return True