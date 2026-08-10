"""通知工厂。同时向所有已配置的渠道推送。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.core.config import get_settings


@runtime_checkable
class Notifier(Protocol):
    enabled: bool
    def send_text_message(self, text: str, **kwargs) -> bool: ...


def get_notifiers() -> list:
    settings = get_settings()
    notifiers = []

    if settings.telegram_bot_token and settings.telegram_chat_id:
        from app.modules.notify.telegram import TelegramNotifier
        notifiers.append(TelegramNotifier())

    if settings.wechat_corp_id and settings.wechat_corp_secret:
        from app.modules.notify.wechat import WeChatNotifier
        notifiers.append(WeChatNotifier())

    return notifiers


def broadcast_text(text: str) -> int:
    """向所有渠道发文本，返回成功数。"""
    return sum(1 for n in get_notifiers() if n.send_text_message(text))


def broadcast_photo(photo_url: str, caption: str, title: str = "") -> int:
    """向所有渠道发图文。各渠道签名不同，这里做适配。"""
    count = 0
    for notifier in get_notifiers():
        try:
            name = notifier.__class__.__name__
            if name == "TelegramNotifier":
                ok = notifier.send_photo_message(photo_url, caption)
            else:
                ok = notifier.send_photo_message(photo_url, title or caption, caption)
            count += bool(ok)
        except Exception:
            continue
    return count
