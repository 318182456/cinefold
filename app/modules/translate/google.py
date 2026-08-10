"""Google 翻译（官方 API）。"""
from __future__ import annotations

import httpx
from loguru import logger

from app.core.config import get_settings

API_URL = "https://translation.googleapis.com/language/translate/v2"


class Google:
    def __init__(self, api_key: str = ""):
        settings = get_settings()
        self.api_key = api_key or settings.google_api_key
        self.proxy = settings.proxy or None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def translate(self, text: str, from_lang: str = "ja", to_lang: str = "zh-CN") -> str:
        if not self.enabled or not text:
            return ""

        try:
            with httpx.Client(timeout=20, proxy=self.proxy) as client:
                response = client.post(
                    API_URL,
                    params={"key": self.api_key},
                    json={
                        "q": text,
                        "source": from_lang,
                        "target": to_lang,
                        "format": "text",
                    },
                )
                response.raise_for_status()
                translations = response.json().get("data", {}).get("translations", [])

            return translations[0].get("translatedText", "") if translations else ""
        except Exception as exc:
            logger.warning(f"Google 翻译异常: {exc}")
            return ""
