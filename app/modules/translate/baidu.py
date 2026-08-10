"""百度翻译。"""
from __future__ import annotations

import hashlib
import random

import httpx
from loguru import logger

from app.core.config import get_settings

API_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"


class Baidu:
    def __init__(self, app_id: str = "", api_key: str = ""):
        settings = get_settings()
        self.app_id = app_id or settings.baidu_app_id
        self.api_key = api_key or settings.baidu_api_key

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.api_key)

    def translate(self, text: str, from_lang: str = "jp", to_lang: str = "zh") -> str:
        """翻译失败时返回原文，避免上层拿到空串。"""
        if not self.enabled or not text:
            return ""

        salt = str(random.randint(32768, 65536))
        sign = hashlib.md5(
            f"{self.app_id}{text}{salt}{self.api_key}".encode()
        ).hexdigest()

        try:
            with httpx.Client(timeout=20) as client:
                response = client.get(
                    API_URL,
                    params={
                        "q": text,
                        "from": from_lang,
                        "to": to_lang,
                        "appid": self.app_id,
                        "salt": salt,
                        "sign": sign,
                    },
                )
                response.raise_for_status()
                data = response.json()

            if "error_code" in data:
                logger.warning(f"百度翻译失败 {data.get('error_code')}: {data.get('error_msg')}")
                return ""

            results = data.get("trans_result") or []
            return "\n".join(item.get("dst", "") for item in results)
        except Exception as exc:
            logger.warning(f"百度翻译异常: {exc}")
            return ""
