"""版本号读取。

根目录的 VERSION 文件是唯一数据源，改版本只需改那一个文件。
构建镜像时会被复制进去；读不到时返回 UNKNOWN_VERSION，
保证在缺文件的环境（如直接 pip 装包）里不会崩。
"""
from __future__ import annotations

from pathlib import Path

# 不写死具体版本号，避免与 VERSION 文件不同步
UNKNOWN_VERSION = "0.0.0"

# app/core/version.py → 项目根
_VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"


def get_version() -> str:
    try:
        version = _VERSION_FILE.read_text(encoding="utf-8").strip()
        return version or UNKNOWN_VERSION
    except OSError:
        return UNKNOWN_VERSION


APP_VERSION = get_version()
