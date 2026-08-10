"""版本号读取。

VERSION 文件是唯一数据源，构建镜像时会被复制进去。读不到时回落到
FALLBACK_VERSION，保证在缺文件的环境（如直接 pip 装包）里不会崩。
"""
from __future__ import annotations

from pathlib import Path

FALLBACK_VERSION = "2.0.0"

# app/core/version.py → 项目根
_VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"


def get_version() -> str:
    try:
        version = _VERSION_FILE.read_text(encoding="utf-8").strip()
        return version or FALLBACK_VERSION
    except OSError:
        return FALLBACK_VERSION


APP_VERSION = get_version()
