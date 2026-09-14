"""翻译工厂。

按 AI → 腾讯 → 百度 → Google 顺序尝试，前者失败自动降级。

腾讯排在三家传统翻译的最前面：AI 那档经常被网关以内容为由拦下（片名
普遍露骨，见 translateai 里那串 REFUSAL 注释），而腾讯/百度/Google 是
翻译 API，不对内容作道德判断，照翻不误 —— 所以 AI 之后这一档才是实际
把活干完的那个。三家里腾讯每月 500 万字符免费且日译中质量尚可，百度
免费额度也够但质量一般，Google 质量最好却没有免费额度（必须绑结算），
按「先免费、后质量」排下来就是这个顺序。
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

    if settings.tencent_secret_id and settings.tencent_secret_key:
        from app.modules.translate.tencent import Tencent
        translators.append(Tencent())

    if settings.baidu_app_id and settings.baidu_api_key:
        from app.modules.translate.baidu import Baidu
        translators.append(Baidu())

    if settings.google_api_key:
        from app.modules.translate.google import Google
        translators.append(Google())

    return translators


def translate(text: str) -> str:
    """翻译文本。全部失败时返回空串，由调用方决定是否保留原文。

    某一家报「服务不可用」不代表下一家也不行 —— 三家是彼此独立的服务，
    所以照常降级往下试。只有在**每一家**都不可用时才抛
    TranslateUnavailable：那才说明问题不在某个供应商，而是整条链路
    （断网、全部挂掉）都不通，上层该停掉整轮而不是继续硬打。
    """
    if not text:
        return ""

    from app.modules.translate.translateai import TranslateUnavailable

    translators = get_translators()
    unavailable = 0

    for translator in translators:
        name = translator.__class__.__name__
        try:
            result = translator.translate(text)
            if result:
                return result
        except TranslateUnavailable as exc:
            unavailable += 1
            logger.debug(f"{name} 服务不可用，尝试下一个: {exc}")
            continue
        except Exception as exc:
            logger.debug(f"{name} 翻译失败，尝试下一个: {exc}")
            continue

    # 每一家都报不可用，才算整条链路不通
    if translators and unavailable == len(translators):
        raise TranslateUnavailable(f"{unavailable} 家翻译服务全部不可用")

    return ""


def is_available() -> bool:
    return bool(get_translators())
