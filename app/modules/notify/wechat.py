"""企业微信应用消息推送。"""
from __future__ import annotations

import time

import httpx
from loguru import logger

from app.core.config import get_settings

API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"


class WeChatNotifier:
    def __init__(
        self,
        corp_id: str = "",
        corp_secret: str = "",
        agent_id: str = "",
        to_user: str = "",
    ):
        settings = get_settings()
        self.corp_id = corp_id or settings.wechat_corp_id
        self.corp_secret = corp_secret or settings.wechat_corp_secret
        self.agent_id = agent_id or settings.wechat_agent_id
        self.to_user = to_user or settings.wechat_to_user or "@all"
        self.proxy = settings.wechat_proxy or settings.proxy or None
        self.banner = settings.wechat_banner
        self.photo = settings.wechat_photo

        self._token = ""
        self._token_expire = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.corp_id and self.corp_secret and self.agent_id)

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=30, proxy=self.proxy)

    # ------------------------------------------------------------------
    def get_access_token(self) -> str:
        """access_token 有效期 7200s，提前 200s 刷新。"""
        if self._token and time.time() < self._token_expire:
            return self._token

        if not self.enabled:
            return ""

        try:
            with self._client() as client:
                response = client.get(
                    f"{API_BASE}/gettoken",
                    params={"corpid": self.corp_id, "corpsecret": self.corp_secret},
                )
                response.raise_for_status()
                data = response.json()

            if data.get("errcode") != 0:
                logger.error(f"获取企业微信 token 失败: {data.get('errmsg')}")
                return ""

            self._token = data.get("access_token", "")
            self._token_expire = time.time() + int(data.get("expires_in", 7200)) - 200
            return self._token
        except Exception as exc:
            logger.error(f"获取企业微信 token 异常: {exc}")
            return ""

    # ------------------------------------------------------------------
    def send_text_message(self, text: str, to_user: str = "") -> bool:
        token = self.get_access_token()
        if not token:
            return False

        try:
            with self._client() as client:
                response = client.post(
                    f"{API_BASE}/message/send",
                    params={"access_token": token},
                    json={
                        "touser": to_user or self.to_user,
                        "msgtype": "text",
                        "agentid": int(self.agent_id),
                        "text": {"content": text},
                    },
                )
                response.raise_for_status()
                data = response.json()

            if data.get("errcode") != 0:
                logger.error(f"企业微信推送失败: {data.get('errmsg')}")
                return False
            return True
        except Exception as exc:
            logger.error(f"企业微信推送异常: {exc}")
            return False

    def send_photo_message(
        self, photo_url: str, title: str = "", description: str = "", url: str = ""
    ) -> bool:
        """图文消息。企业微信要求图片可外网访问。"""
        token = self.get_access_token()
        if not token:
            return False

        # 未开启横幅时退化为纯文本，省一次请求
        if not self.banner:
            return self.send_text_message(f"{title}\n{description}")

        try:
            with self._client() as client:
                response = client.post(
                    f"{API_BASE}/message/send",
                    params={"access_token": token},
                    json={
                        "touser": self.to_user,
                        "msgtype": "news",
                        "agentid": int(self.agent_id),
                        "news": {
                            "articles": [{
                                "title": title,
                                "description": description,
                                "url": url or photo_url,
                                "picurl": photo_url or self.photo,
                            }]
                        },
                    },
                )
                response.raise_for_status()
                data = response.json()

            if data.get("errcode") != 0:
                logger.error(f"企业微信图文推送失败: {data.get('errmsg')}")
                return False
            return True
        except Exception as exc:
            logger.error(f"企业微信图文推送异常: {exc}")
            return False

    def test_connection(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "未配置企业微信参数"
        return (True, "连接成功") if self.get_access_token() else (False, "获取 token 失败")
