"""AI 助手。带工具调用的对话入口，用于直接问系统当前情况。"""
from __future__ import annotations

from .agent import ChatAgent, ask

__all__ = ["ChatAgent", "ask"]
