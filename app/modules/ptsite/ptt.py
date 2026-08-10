"""PTTime。"""
from __future__ import annotations

from app.core.config import get_settings
from app.modules.ptsite.nexus import NexusSite


class PTT(NexusSite):
    name = "PTT"
    host = "https://www.pttime.org"

    def __init__(self, cookie: str = "", host: str = ""):
        super().__init__(cookie or get_settings().ptt_cookie, host)
