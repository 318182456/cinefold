"""Emby 媒体库查询。"""
from __future__ import annotations

import httpx
from loguru import logger

from app.core.config import get_settings


class Emby:
    def __init__(self, url: str = "", api_key: str = ""):
        settings = get_settings()
        self.url = (url or settings.emby_url).rstrip("/")
        self.api_key = api_key or settings.emby_api_key

    def search(self, keyword: str) -> bool:
        """番号是否已入库。"""
        if not self.url or not self.api_key:
            return False

        try:
            with httpx.Client(timeout=15) as client:
                response = client.get(
                    f"{self.url}/emby/Items",
                    params={
                        "api_key": self.api_key,
                        "SearchTerm": keyword,
                        "IncludeItemTypes": "Movie",
                        "Recursive": "true",
                        "Limit": 5,
                    },
                )
                response.raise_for_status()
                items = response.json().get("Items", [])

            # Emby 的模糊搜索会命中相近番号，这里要求名称真正包含关键词
            keyword_upper = keyword.upper()
            for item in items:
                if keyword_upper in (item.get("Name") or "").upper():
                    logger.debug(f"[{keyword}] Emby 已存在")
                    return True
            return False
        except Exception as exc:
            logger.warning(f"查询 Emby 失败: {exc}")
            return False

    def test_connection(self) -> tuple[bool, str]:
        if not self.url or not self.api_key:
            return False, "未配置 Emby 地址或 API Key"
        try:
            with httpx.Client(timeout=15) as client:
                response = client.get(
                    f"{self.url}/emby/System/Info", params={"api_key": self.api_key}
                )
                response.raise_for_status()
                return True, f"连接成功，Emby {response.json().get('Version', '')}"
        except Exception as exc:
            return False, str(exc)
