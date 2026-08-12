"""新版本检测。

检测源从 GHCR 镜像标签换成了 GitHub Releases —— 热更新要下载的 zip 挂在
release 上，用同一个源判断"有没有新版"才不会出现"提示有更新但装不了"。

实现全在 app.services.upgrade 里，这里只保留旧的函数名做转发，
外部调用方（app/api/endpoints/config.py）不用改。
"""
from __future__ import annotations

from app.core.overlay import parse_version  # noqa: F401  旧调用方还在用
from app.services.upgrade import check_update  # noqa: F401

__all__ = ["check_update", "parse_version"]
