"""翻译工厂。

按 AI → 百度 → Google 顺序尝试，前者失败自动降级。
"""
from __future__ import annotations

from loguru import logger

from app.core.config import get_settings


def get_translators() -> list:
    settings = get_settings()
    translators = []

    if settings.openai_url and settings.openai_api_key:
        from app.modules.translate.translateai import TranslateAI
        translators.append(TranslateAI())

    if settings.baidu_app_id and settings.baidu_api_key:
        from app.modules.translate.baidu import Baidu
        translators.append(Baidu())

    if settings.google_api_key:
        from app.modules.translate.google import Google
        translators.append(Google())

    return translators


def translate(text: str) -> str:
    """翻译文本。全部失败时返回空串，由调用方决定是否保留原文。"""
    if not text:
        return ""

    for translator in get_translators():
        try:
            result = translator.translate(text)
            if result:
                return result
        except Exception as exc:
            logger.debug(f"{translator.__class__.__name__} 翻译失败，尝试下一个: {exc}")
            continue

    return ""


def is_available() -> bool:
    return bool(get_translators())
