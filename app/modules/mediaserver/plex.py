"""Plex 媒体库查询。"""
from __future__ import annotations

from loguru import logger

from app.core.config import get_settings


class Plex:
    def __init__(self, url: str = "", token: str = ""):
        settings = get_settings()
        self.url = (url or settings.plex_url).rstrip("/")
        self.token = token or settings.plex_token
        self._server = None

    def _connect(self):
        if self._server is not None:
            return self._server
        try:
            from plexapi.server import PlexServer
            self._server = PlexServer(self.url, self.token, timeout=15)
        except Exception as exc:
            logger.warning(f"连接 Plex 失败: {exc}")
            self._server = None
        return self._server

    def search(self, keyword: str) -> bool:
        if not self.url or not self.token:
            return False

        server = self._connect()
        if server is None:
            return False

        try:
            keyword_upper = keyword.upper()
            for item in server.library.search(title=keyword, limit=5):
                if keyword_upper in (item.title or "").upper():
                    logger.debug(f"[{keyword}] Plex 已存在")
                    return True
            return False
        except Exception as exc:
            logger.warning(f"查询 Plex 失败: {exc}")
            return False

    def test_connection(self) -> tuple[bool, str]:
        if not self.url or not self.token:
            return False, "未配置 Plex 地址或 Token"
        server = self._connect()
        if server is None:
            return False, "连接失败，请检查地址与 Token"
        try:
            return True, f"连接成功，Plex {server.version}"
        except Exception as exc:
            return False, str(exc)
