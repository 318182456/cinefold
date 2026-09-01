"""自建刮削：元数据 → NFO + 图片 + 硬链接。

替代外部刮削工具（MDCng 等）那条链路。模块划分：

    nfo.py       生成 Kodi/Emby 认的 <movie> NFO
    naming.py    产物目录与文件名的模板渲染，兼容 MDCng 的模板语法
    images.py    海报/背景/剧照落盘到影片目录

编排在 services/scrape.py，它把这三块与既有的 ladysite（元数据）、
translate（标题翻译）、subtitle（字幕）、review（AI 看点）串起来。
"""
from __future__ import annotations

from app.modules.scrape.nfo import NfoData, render, write

__all__ = ["NfoData", "render", "write"]
