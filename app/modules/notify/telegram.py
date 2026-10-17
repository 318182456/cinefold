"""Telegram 消息推送与上行消息接收。

直接调 Bot API，不引入 telebot 依赖。上行消息支持两种方式：
webhook 由 Telegram 回调本机（需公网 HTTPS），polling 靠 getUpdates 主动取。
"""
from __future__ import annotations

import re
import time

import httpx
from loguru import logger

from app.core.config import get_settings

API_BASE = "https://api.telegram.org"


class _ConnectRetryTransport(httpx.BaseTransport):
    """兼容直连和 SOCKS/HTTP 代理，只对请求发出前的建连失败重试一次。"""
    def __init__(self, proxy: str | None):
        self._transport = httpx.HTTPTransport(proxy=proxy, trust_env=False)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return self._transport.handle_request(request)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            time.sleep(0.5)
            return self._transport.handle_request(request)

    def close(self) -> None:
        self._transport.close()


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

    def _client(self, timeout: float = 30) -> httpx.Client:
        # 只重试建连阶段：读取响应超时可能意味着消息已经发出，不能自动重发。
        # 显式使用设置中的代理，避免继承容器里意外设置的代理变量。
        transport = _ConnectRetryTransport(self.proxy)
        return httpx.Client(timeout=httpx.Timeout(timeout, connect=min(timeout, 10)),
                            transport=transport, trust_env=False)

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc)
        if self.token:
            message = message.replace(self.token, '[token]')
        message = re.sub(r'(?<=//)[^/@\s]+:[^/@\s]+@', '[credentials]@', message)
        if isinstance(exc, httpx.ConnectError) and any(word in message.lower() for word in ('ssl', 'tls', 'handshake')):
            route = '经代理' if self.proxy else '直连'
            return f'TLS 握手失败（{route}，建连重试后仍失败，请检查代理节点或网络链路）：{message}'
        return message

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
            logger.error(f"Telegram 发送文本失败: {self._safe_error(exc)}")
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
            logger.error(f"Telegram 发图异常: {self._safe_error(exc)}，降级为文本")
            return self.send_text_message(caption, chat_id)

    def send_photo_bytes(self, photo: bytes, caption: str = '', chat_id: str = '') -> bool:
        """Upload saved artwork directly, without a publicly accessible media URL."""
        if not self.enabled:
            return False
        payload = {'chat_id':chat_id or self.chat_id,'caption':caption,'parse_mode':'HTML'}
        if self.spoiler:
            payload['has_spoiler'] = 'true'
        try:
            with self._client() as client:
                response = client.post(self._api('sendPhoto'), data=payload,
                    files={'photo':('cover.jpg',photo,'image/jpeg')})
                response.raise_for_status()
                if not response.json().get('ok',False):
                    raise ValueError('Telegram 未接受图片消息')
            return True
        except (httpx.ReadTimeout,httpx.ReadError,httpx.WriteTimeout,httpx.WriteError):
            # The upload may already have been delivered; do not duplicate it.
            logger.warning('Telegram 图片发送结果不确定，未自动重发，请查看聊天记录')
            return False
        except Exception as exc:
            logger.error(f'Telegram 图片上传失败: {self._safe_error(exc)}，降级为文本')
            return self.send_text_message(caption,chat_id)

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
            logger.error(f"Telegram 回复失败: {self._safe_error(exc)}")
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
            logger.warning(f"下载图片失败 {url}: {self._safe_error(exc)}")
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
            return False, self._safe_error(exc)

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
            logger.warning(f"获取 webhook 信息失败: {self._safe_error(exc)}")
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
            return False, self._safe_error(exc)

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
            return False, self._safe_error(exc)

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
        try:
            with self._client(timeout + 15) as client:
                response = client.post(self._api("getUpdates"), json=params)
                response.raise_for_status()
                return response.json().get("result") or []
        except httpx.HTTPError as exc:
            # 上层轮询线程会记录异常，避免其中带出 Bot API URL 内的 token。
            raise RuntimeError(self._safe_error(exc)) from None
