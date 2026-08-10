"""Telegram 消息推送与上行消息接收。

直接调 Bot API，不引入 telebot 依赖。上行消息支持两种方式：
webhook 由 Telegram 回调本机（需公网 HTTPS），polling 靠 getUpdates 主动取。
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

    # ------------------------------------------------------------------
    # 上行消息接收
    # ------------------------------------------------------------------
    def get_webhook_info(self) -> dict:
        """当前 webhook 状态。失败时返回空字典。"""
        if not self.token:
            return {}
        try:
            with self._client() as client:
                response = client.get(self._api("getWebhookInfo"))
                response.raise_for_status()
                return response.json().get("result") or {}
        except Exception as exc:
            logger.warning(f"获取 webhook 信息失败: {exc}")
            return {}

    def set_webhook(self, url: str) -> tuple[bool, str]:
        """把 webhook 指向 url。"""
        if not self.token:
            return False, "未配置 Telegram Bot Token"
        if not url:
            return False, "外网地址为空"
        try:
            with self._client() as client:
                response = client.post(
                    self._api("setWebhook"),
                    # 只关心消息，其余更新类型不必推过来
                    json={"url": url, "allowed_updates": ["message", "edited_message"]},
                )
                payload = response.json()
            if not payload.get("ok"):
                return False, payload.get("description", "设置失败")
            return True, "webhook 设置成功"
        except Exception as exc:
            return False, str(exc)

    def delete_webhook(self, drop_pending: bool = False) -> tuple[bool, str]:
        """删除 webhook。切到 polling 前必须先删，两者互斥。"""
        if not self.token:
            return False, "未配置 Telegram Bot Token"
        try:
            with self._client() as client:
                response = client.post(
                    self._api("deleteWebhook"),
                    json={"drop_pending_updates": drop_pending},
                )
                payload = response.json()
            if not payload.get("ok"):
                return False, payload.get("description", "删除失败")
            return True, "webhook 已删除"
        except Exception as exc:
            return False, str(exc)

    def get_updates(self, offset: int = 0, timeout: int = 30) -> list[dict]:
        """长轮询拉取更新。

        读超时要比 timeout 更长，否则长连接会被客户端提前掐断。
        """
        if not self.token:
            return []
        params = {
            "timeout": timeout,
            "allowed_updates": ["message", "edited_message"],
        }
        if offset:
            params["offset"] = offset
        with httpx.Client(timeout=timeout + 15, proxy=self.proxy) as client:
            response = client.post(self._api("getUpdates"), json=params)
            response.raise_for_status()
            return response.json().get("result") or []
