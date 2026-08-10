"""Jellyfin 媒体库查询。

接口与 Emby 基本同源，差异在于鉴权头与用户维度查询。
"""
from __future__ import annotations

import httpx
from loguru import logger

from app.core.config import get_settings


class Jellyfin:
    def __init__(self, url: str = "", api_key: str = "", user: str = ""):
        settings = get_settings()
        self.url = (url or settings.jellyfin_url).rstrip("/")
        self.api_key = api_key or settings.jellyfin_api_key
        self.user = user or settings.jellyfin_user
        self._user_id: str | None = None

    def _get_user_id(self) -> str:
        """Jellyfin 查询需要 userId，用配置的用户名换取。"""
        if self._user_id is not None:
            return self._user_id

        self._user_id = ""
        try:
            with httpx.Client(timeout=15) as client:
                response = client.get(
                    f"{self.url}/Users",
                    headers={"X-Emby-Token": self.api_key},
                )
                response.raise_for_status()
                for user in response.json():
                    if not self.user or user.get("Name") == self.user:
                        self._user_id = user.get("Id", "")
                        break
        except Exception as exc:
            logger.warning(f"获取 Jellyfin 用户失败: {exc}")
        return self._user_id

    def search(self, keyword: str) -> bool:
        if not self.url or not self.api_key:
            return False

        try:
            params = {
                "searchTerm": keyword,
                "includeItemTypes": "Movie",
                "recursive": "true",
                "limit": 5,
            }
            user_id = self._get_user_id()
            if user_id:
                params["userId"] = user_id

            with httpx.Client(timeout=15) as client:
                response = client.get(
                    f"{self.url}/Items",
                    headers={"X-Emby-Token": self.api_key},
                    params=params,
                )
                response.raise_for_status()
                items = response.json().get("Items", [])

            keyword_upper = keyword.upper()
            for item in items:
                if keyword_upper in (item.get("Name") or "").upper():
                    logger.debug(f"[{keyword}] Jellyfin 已存在")
                    return True
            return False
        except Exception as exc:
            logger.warning(f"查询 Jellyfin 失败: {exc}")
            return False

    def test_connection(self) -> tuple[bool, str]:
        if not self.url or not self.api_key:
            return False, "未配置 Jellyfin 地址或 API Key"
        try:
            with httpx.Client(timeout=15) as client:
                response = client.get(
                    f"{self.url}/System/Info", headers={"X-Emby-Token": self.api_key}
                )
                response.raise_for_status()
                return True, f"连接成功，Jellyfin {response.json().get('Version', '')}"
        except Exception as exc:
            return False, str(exc)
