"""AI 翻译。走 OpenAI 兼容接口，可对接任何兼容服务。"""
from __future__ import annotations

import httpx
from loguru import logger

from app.core.config import get_settings

PROMPT = (
    "你是影片标题翻译助手。将用户给出的日文标题翻译成简体中文，"
    "只输出译文本身，不要解释、不要加引号、不要输出多余内容。"
)


class TranslateAI:
    def __init__(self, url: str = "", model: str = "", api_key: str = ""):
        settings = get_settings()
        self.url = (url or settings.openai_url).rstrip("/")
        self.model = model or settings.openai_model or "gpt-4o-mini"
        self.api_key = api_key or settings.openai_api_key
        self.proxy = settings.proxy or None

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_key)

    def translate(self, text: str, from_lang: str = "ja", to_lang: str = "zh") -> str:
        if not self.enabled or not text:
            return ""

        endpoint = self.url
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"

        try:
            with httpx.Client(timeout=60, proxy=self.proxy) as client:
                response = client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": PROMPT},
                            {"role": "user", "content": text},
                        ],
                        "temperature": 0.2,
                    },
                )
                response.raise_for_status()
                choices = response.json().get("choices") or []

            if not choices:
                return ""
            return (choices[0].get("message", {}).get("content") or "").strip()
        except Exception as exc:
            logger.warning(f"AI 翻译异常: {exc}")
            return ""
