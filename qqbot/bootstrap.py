from __future__ import annotations

import asyncio
import logging
import os
import time

from .agent_client import AgentClient, AgentError

log = logging.getLogger(__name__)

# 后端 set_prompt 只有识别到这个前缀才会「直接覆盖」提示词，
# 否则会把参数当成 prompt_loader 的文件名去加载。
PROMPT_OVERWRITE = "PROMPTOVERWRITE"
SET_PROMPT_PREFIX = "#bot prompt "


def load_prompt(path: str) -> str:
    """读取提示词文件；不存在或为空返回空串。"""
    if not path:
        return ""
    if not os.path.exists(path):
        log.info("提示词文件不存在：%s", path)
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return fp.read()
    except OSError as exc:
        log.warning("读取提示词文件失败：%s", exc)
        return ""


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
) -> bool:
    """启动时把 prompt.md 的内容写入后端 Agent 的系统提示词。

    流程：
      1. 读取 prompt.md，为空则跳过（允许先留空，之后再补）
      2. 以 ``#bot prompt PROMPTOVERWRITE<内容>`` 投递给后端
      3. 轮询 answer，等后端处理完（status 回 0）
      4. 把残留响应消费干净，防止混入第一条用户回复

    返回是否成功投递并收到回应。失败不抛异常，只记日志——
    初始化失败不应该阻止机器人上线。
    """
    text = load_prompt(prompt_path)
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