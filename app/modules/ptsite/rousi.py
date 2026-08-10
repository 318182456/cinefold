"""Rousi。"""
from __future__ import annotations

from app.core.config import get_settings
from app.modules.ptsite.nexus import NexusSite


class Rousi(NexusSite):
    name = "Rousi"
    host = "https://rousi.zip"

    def __init__(self, cookie: str = "", host: str = ""):
        super().__init__(cookie or get_settings().rousi_cookie, host)
