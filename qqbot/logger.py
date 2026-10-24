from __future__ import annotations

import logging
import os
import sys


def _force_utf8_streams() -> None:
    """Windows 控制台默认 GBK，遇到 emoji/特殊符号会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    """初始化日志：同时输出到控制台与 logs/qqbot.log。"""
    _force_utf8_streams()

    level_value = getattr(logging, str(level).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level_value)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(
            os.path.join(log_dir, "qqbot.log"), encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:  # 日志目录不可写时不影响主流程
        logging.getLogger(__name__).warning("无法写入日志文件: %s", exc)

    logging.getLogger("aiohttp").setLevel(logging.WARNING)