"""Google 翻译（官方 API）。"""
from __future__ import annotations

import httpx
from loguru import logger

from app.core.config import get_settings

API_URL = "https://translation.googleapis.com/language/translate/v2"


class Google:
    # 未开通结算的告警只打一次，否则每条番号都要刷一行
    _quota_warned = False

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
                # 403 得单独认出来：Google 翻译没有免费额度，项目没绑结算账号
                # 时每次调用都回 403 userRateLimitExceeded。文案却是「超出速率
                # 限制」，看着像临时限流，实际重试一万次也一样 —— 45000 条番号
                # 会把这句无从下手的告警刷满日志。
                #
                # 判据：languages 那个免费端点返回 200（key 有效、API 已启用），
                # 而 translate/detect 这两个计费端点一律 403。
                if response.status_code == 403:
                    if not Google._quota_warned:
                        Google._quota_warned = True
                        logger.warning(
                            "Google 翻译返回 403：该 API Key 所属项目未开通结算。"
                            "Google 翻译无免费额度，需在 Cloud Console 绑定结算账号后才能用。"
                            "本次运行不再重复提示"
                        )
                    return ""

                response.raise_for_status()
                translations = response.json().get("data", {}).get("translations", [])

            return translations[0].get("translatedText", "") if translations else ""
        except Exception as exc:
            logger.warning(f"Google 翻译异常: {exc}")
            return ""
