"""Discord Webhook 通知。复用通知工厂，不开启机器人常驻连接。"""
import re
from html import unescape

import httpx
from loguru import logger

from app.core.config import get_settings


class DiscordNotifier:
    def __init__(self):
        settings = get_settings()
        self.url = settings.discord_webhook_url
        self.proxy = settings.proxy or None

    @property
    def enabled(self):
        return bool(self.url)

    def _send(self, payload):
        if not self.enabled:
            return False
        try:
            with httpx.Client(timeout=20, proxy=self.proxy, trust_env=False) as client:
                response = client.post(self.url, json={**payload, "allowed_mentions": {"parse": []}})
                response.raise_for_status()
            return True
        except Exception as exc:
            # Webhook URL 本身是凭证，不把 HTTP 异常里的 URL 写进日志。
            logger.warning(f"Discord 通知失败：{type(exc).__name__}")
            return False

    def send_text_message(self, text: str, **kwargs):
        plain = unescape(re.sub(r"<[^>]+>", "", text))
        return self._send({"content": plain[:2000] or "cinefold 通知"})

    def send_photo_message(self, photo_url: str, title: str, caption: str):
        embed = {"title": unescape(re.sub(r"<[^>]+>", "", title))[:256],
                 "description": unescape(re.sub(r"<[^>]+>", "", caption))[:4096]}
        if photo_url.startswith(("http://", "https://")):
            embed["image"] = {"url": photo_url}
        return self._send({"embeds": [embed]})
