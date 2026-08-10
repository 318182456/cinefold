"""NicePT。"""
from __future__ import annotations

from app.core.config import get_settings
from app.modules.ptsite.nexus import NexusSite


class NicePT(NexusSite):
    name = "NicePT"
    host = "https://www.nicept.net"

    def __init__(self, cookie: str = ""):
        super().__init__(cookie or get_settings().nicept_cookie)
