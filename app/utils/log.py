"""日志配置。

同时输出到控制台与文件，文件按天滚动，供 Web UI 的日志页读取。
"""
from __future__ import annotations

import os
import re
import sys
from collections import deque
from pathlib import Path

from loguru import logger

LOG_DIR = Path(os.getenv("LOG_DIR", "/data/logs"))
LOG_FILE = LOG_DIR / "cinefold.log"

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


_LOG_NAME = re.compile(r'cinefold(?:\.\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d+)?\.log')
_LEVELS = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}


def _log_path(filename: str) -> Path:
    """只允许读取日志目录内的当前或滚动日志，不跟随外部链接。"""
    if not _LOG_NAME.fullmatch(filename):
        raise ValueError('日志文件名无效')
    path = LOG_DIR / filename
    if path.is_symlink() or path.resolve().parent != LOG_DIR.resolve():
        raise ValueError('日志文件路径无效')
    return path


def list_log_files() -> list[dict]:
    """列出仍保留的日志文件，不向页面暴露服务器绝对路径。"""
    files = []
    for path in LOG_DIR.glob('cinefold*.log'):
        if not _LOG_NAME.fullmatch(path.name) or path.is_symlink():
            continue
        try:
            if not path.is_file():
                continue
            info = path.stat()
        except OSError:
            continue
        files.append({'name': path.name, 'size': info.st_size,
                      'modified_at': info.st_mtime, 'current': path.name == LOG_FILE.name})
    return sorted(files, key=lambda item: (item['current'], item['name']), reverse=True)


def read_logs(lines: int = 200, keyword: str = '', filename: str = '', level: str = '') -> list[str]:
    """流式过滤后保留最后若干行，兼容当前日志和指定归档文件。"""
    if level and level not in _LEVELS:
        raise ValueError('日志级别无效')
    path = _log_path(filename or LOG_FILE.name)
    count = max(1, min(5000, int(lines)))
    needle = keyword.casefold()
    try:
        with path.open(encoding='utf-8', errors='replace') as stream:
            return list(deque((line.rstrip('\r\n') for line in stream
                               if (not needle or needle in line.casefold())
                               and (not level or re.search(r'\|\s*' + level + r'\s*\|', line))), maxlen=count))
    except FileNotFoundError:
        if filename:
            raise
        return []
