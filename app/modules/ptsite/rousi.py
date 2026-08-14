"""Rousi。

新站（rousi.pro）是前后端分离架构，不再是 NexusPHP：
- 认证用 `Authorization: Bearer <token>`
- 数据走 `/api/torrent/search?query=<番号>`，返回 JSON

token 是有期限的 JWT。配了用户名密码时会自动登录换取并在过期后重登，
只填 token 也能用，但到期后需要手动更新。
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time

import httpx
from loguru import logger

from app.core.config import get_settings
from app.modules.ptsite import convert_to_mb, download_seed_by_url
from app.schemas.torrent import Torrent
from app.utils import get_true_code
from app.utils.filters import has_chinese, has_uc, has_uhd

DEFAULT_HOST = "https://rousi.pro"
# 距过期不足这么久就提前续期，避免请求发到一半失效
TOKEN_REFRESH_MARGIN = 300


class Rousi:
    name = "Rousi"

    # 最近一次 search 是否失败，见 ptsite.crawling_checked
    search_failed = False

    # 登录换来的 token 存在类上：get_sites() 每次搜索都会新建实例，
    # 存在实例里等于每搜一个番号就重新登录一次
    _shared_token: str = ""
    _lock = threading.Lock()

    def __init__(
        self,
        token: str = "",
        host: str = "",
        passkey: str = "",
        username: str = "",
        password: str = "",
    ):
        settings = get_settings()
        self.host = (host or os.getenv("ROUSI_HOST", "") or DEFAULT_HOST).rstrip("/")
        self.passkey = passkey or settings.rousi_passkey
        self.username = username or settings.rousi_username
        self.password = password or settings.rousi_password
        self.proxy = settings.proxy or None

        self._token = token or settings.rousi_token

    @classmethod
    def reset_token_cache(cls) -> None:
        """配置变更后调用，避免继续用旧账号换来的 token。"""
        with Rousi._lock:
            Rousi._shared_token = ""

    @property
    def enabled(self) -> bool:
        return bool(self._token or self._shared_token
                    or (self.username and self.password))

    # ------------------------------------------------------------------
    @property
    def token(self) -> str:
        """返回可用的 token，必要时自动登录换取。"""
        # 手动配的优先，其次是上次登录缓存下来的
        for candidate in (self._token, Rousi._shared_token):
            if candidate and not self._is_expiring(candidate):
                return candidate

        if not (self.username and self.password):
            # 只配了 token，过期也只能原样返回，由调用方看到 401
            return self._token

        with Rousi._lock:
            # 可能已被其他线程刷新
            if Rousi._shared_token and not self._is_expiring(Rousi._shared_token):
                return Rousi._shared_token
            fresh = self._login()
            if fresh:
                Rousi._shared_token = fresh
                return fresh
        return self._token

    @staticmethod
    def _is_expiring(token: str) -> bool:
        """解析 JWT 的 exp 判断是否临近过期。解不出时视为有效。"""
        try:
            payload = token.split(".")[1]
            padded = payload + "=" * (-len(payload) % 4)
            exp = json.loads(base64.urlsafe_b64decode(padded)).get("exp")
            return bool(exp) and time.time() > float(exp) - TOKEN_REFRESH_MARGIN
        except Exception:
            return False

    def _login(self) -> str:
        """用用户名密码换取新 token。"""
        try:
            with self._client() as client:
                response = client.post(
                    f"{self.host}/api/auth/login",
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "Referer": f"{self.host}/login",
                        "User-Agent": self._user_agent(),
                    },
                    json={
                        "identifier": self.username,
                        "password": self.password,
                        "remember_me": True,
                    },
                )
                payload = response.json()
        except Exception as exc:
            logger.warning(f"[Rousi] 登录请求失败: {exc}")
            return ""

        if payload.get("code") != 0:
            logger.error(f"[Rousi] 登录失败: {payload.get('message')}")
            return ""

        data = payload.get("data") or {}
        token = data.get("token") or payload.get("token") or ""
        if token:
            logger.info("[Rousi] 已通过用户名密码获取新 token")
        else:
            logger.warning(f"[Rousi] 登录响应中没有 token: {list(data.keys())}")
        return token

    @staticmethod
    def _user_agent() -> str:
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": self._user_agent(),
            "Referer": f"{self.host}/",
        }

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=30,
            proxy=self.proxy,
            follow_redirects=True,
            verify=False,
            trust_env=False,
        )

    def _get(self, path: str, params: dict | None = None) -> dict:
        with self._client() as client:
            response = client.get(
                f"{self.host}/api{path}", headers=self._headers(), params=params
            )
            response.raise_for_status()
            return response.json()

    # ------------------------------------------------------------------
    def check_status(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "未配置 Rousi Token"
        try:
            payload = self._get("/me")
            if payload.get("code") != 0:
                return False, payload.get("message", "鉴权失败")
            stats = (payload.get("data") or {}).get("stats") or {}
            return True, f"连接成功，用户 {stats.get('username', '')}"
        except Exception as exc:
            return False, str(exc)[:60]

    # ------------------------------------------------------------------
    def search(self, keyword: str) -> list[Torrent]:
        self.search_failed = False
        if not self.enabled:
            self.search_failed = True
            return []

        code = get_true_code(keyword) or keyword
        try:
            payload = self._get("/torrent/search", {"query": code, "page": 0})
        except Exception as exc:
            logger.warning(f"[Rousi] 搜索异常: {exc}")
            self.search_failed = True
            return []

        if payload.get("code") != 0:
            logger.warning(f"[Rousi] 搜索失败: {payload.get('message')}")
            self.search_failed = True
            return []

        items = self._extract_items(payload.get("data"))
        return [self._convert(item, code) for item in items]

    @staticmethod
    def _extract_items(data) -> list[dict]:
        if isinstance(data, dict):
            return data.get("torrents") or data.get("list") or []
        return data if isinstance(data, list) else []

    def _convert(self, item: dict, code: str) -> Torrent:
        title = str(item.get("name") or item.get("title") or "")
        attributes = item.get("attributes") or {}
        promotion = item.get("promotion") or {}

        # 免费判定：促销类型为 free，或魔力值价格为 0
        discount = str(promotion.get("type") or promotion.get("discount") or "").lower()
        free = "free" in discount or str(item.get("price", "")) == "0"

        resolution = str(attributes.get("resolution") or "")

        return Torrent(
            id=int(item.get("id") or 0),
            site=self.name,
            title=title,
            # size 是字节
            size_mb=round(int(item.get("size") or 0) / 1024 / 1024, 2),
            seeders=int(item.get("seeders") or 0),
            chinese=has_chinese(title),
            uc=has_uc(title),
            uhd=has_uhd(title) or resolution in ("2160p", "4K", "8K"),
            free=free,
            download_url=self._build_download_url(item.get("id"), item.get("info_hash")),
            detail_url=f"{self.host}/torrent/{item.get('id')}",
            code=code,
        )

    def _build_download_url(self, torrent_id, info_hash: str = "") -> str:
        """优先用站点下载接口；无 passkey 时退回磁力链接。"""
        if torrent_id and self.passkey:
            return f"{self.host}/api/torrent/{torrent_id}/download?passkey={self.passkey}"
        if info_hash:
            return f"magnet:?xt=urn:btih:{info_hash}"
        return ""

    def download_seed(self, torrent: Torrent) -> bytes | None:
        url = torrent.download_url
        if not url or url.startswith("magnet:"):
            return None
        return download_seed_by_url(
            url, headers=self._headers(), proxy=self.proxy
        )
