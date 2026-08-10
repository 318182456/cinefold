"""日志配置。

同时输出到控制台与文件，文件按天滚动，供 Web UI 的日志页读取。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

LOG_DIR = Path(os.getenv("LOG_DIR", "/data/logs"))
LOG_FILE = LOG_DIR / "byte-muse.log"

CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>"
)

FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}"


def setup_loguru_logger(level: str = "") -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()

    logger.remove()
    logger.add(sys.stdout, format=CONSOLE_FORMAT, level=level, colorize=True)
    logger.add(
        LOG_FILE,
        format=FILE_FORMAT,
        level=level,
        rotation="00:00",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,  # 调度器多线程写入需要
    )


def read_logs(lines: int = 200, keyword: str = "") -> list[str]:
    """读取最近的日志行，供 API 返回。"""
    if not LOG_FILE.exists():
        return []

    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    if keyword:
        content = [line for line in content if keyword.lower() in line.lower()]
    return content[-lines:]
