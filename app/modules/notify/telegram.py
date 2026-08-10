"""Telegram 消息推送。

直接调 Bot API，不引入 telebot 依赖——只需要发消息，没必要跑 polling。
"""
from __future__ import annotations

import httpx
from loguru import logger

from app.core.config import get_settings

API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(self, token: str = "", chat_id: str = "", spoiler: bool | None = None):
        settings = get_settings()
        self.token = token or settings.telegram_bot_token
        self.chat_id = chat_id or settings.telegram_chat_id
        self.spoiler = settings.telegram_spoiler if spoiler is None else spoiler
        self.proxy = settings.proxy or None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _api(self, method: str) -> str:
        return f"{API_BASE}/bot{self.token}/{method}"

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=30, proxy=self.proxy)

    # ------------------------------------------------------------------
    def send_text_message(self, text: str, chat_id: str = "") -> bool:
        if not self.enabled:
            return False
        try:
            with self._client() as client:
                response = client.post(
                    self._api("sendMessage"),
                    json={
                        "chat_id": chat_id or self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                response.raise_for_status()
            return True
        except Exception as exc:
            logger.error(f"Telegram 发送文本失败: {exc}")
            return False

    def send_photo_message(
        self, photo_url: str, caption: str = "", chat_id: str = ""
    ) -> bool:
        """发图。失败时降级为纯文本，避免消息丢失。"""
        if not self.enabled:
            return False

        payload = {
            "chat_id": chat_id or self.chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if self.spoiler:
            payload["has_spoiler"] = True

        try:
            with self._client() as client:
                response = client.post(self._api("sendPhoto"), json=payload)
                if response.status_code >= 400:
                    logger.warning(
                        f"Telegram 发图失败({response.status_code})，降级为文本"
                    )
                    return self.send_text_message(caption, chat_id)
            return True
        except Exception as exc:
            logger.error(f"Telegram 发图异常: {exc}，降级为文本")
            return self.send_text_message(caption, chat_id)

    def reply_text_message(self, text: str, message_id: int, chat_id: str = "") -> bool:
        if not self.enabled:
            return False
        try:
            with self._client() as client:
                response = client.post(
                    self._api("sendMessage"),
                    json={
                        "chat_id": chat_id or self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "reply_to_message_id": message_id,
                    },
                )
                response.raise_for_status()
            return True
        except Exception as exc:
            logger.error(f"Telegram 回复失败: {exc}")
            return False

    def download_image(self, url: str) -> bytes | None:
        """部分图源有防盗链，需要带 Referer 才能取到。"""
        try:
            with self._client() as client:
                response = client.get(
                    url,
                    headers={"Referer": "https://www.javbus.com/"},
                    follow_redirects=True,
                )
                response.raise_for_status()
                return response.content
        except Exception as exc:
            logger.warning(f"下载图片失败 {url}: {exc}")
            return None

    def test_connection(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "未配置 Telegram Bot Token 或 Chat ID"
        try:
            with self._client() as client:
                response = client.get(self._api("getMe"))
                response.raise_for_status()
                name = response.json().get("result", {}).get("username", "")
                return True, f"连接成功，Bot @{name}"
        except Exception as exc:
            return False, str(exc)
