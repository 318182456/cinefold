"""Rousi。

新站（rousi.pro）是前后端分离架构，不再是 NexusPHP：
- 认证用 `Authorization: Bearer <API Key>`
- 数据走 `/api/torrent/search?query=<番号>`，返回 JSON

只认站点签发的个人 API Key。那是一把通用 Key，同一把也能给 MoviePilot、
PT-depiler 等工具用，按授予的权限调用 —— 本模块需要「读取账户资料」
「读取与搜索种子」「下载种子」三项。

早先支持过账号密码登录换 JWT、以及 Tracker Passkey 拼下载地址，现已全部
去掉：Key 不过期、不需要续期，也就没有登录、token 缓存、过期判断这些环节。
只填 Key 一项即可。

下载地址把 Key 放在请求路径里（站点上游下载协议如此设计），不是查询参数。
这个地址等同于凭据，别外传，日志里也不打印完整地址。
"""
from __future__ import annotations

import os

import httpx
from loguru import logger

from app.core.config import get_settings
from app.modules.ptsite import download_seed_by_url
from app.schemas.torrent import Torrent
from app.utils import get_true_code
from app.utils.filters import has_chinese, has_uc, has_uhd

DEFAULT_HOST = "https://rousi.pro"


class Rousi:
    name = "Rousi"

    # 最近一次 search 是否失败，见 ptsite.crawling_checked
    search_failed = False

    def __init__(self, apikey: str = "", host: str = ""):
        settings = get_settings()
        self.host = (host or os.getenv("ROUSI_HOST", "") or DEFAULT_HOST).rstrip("/")
        self.apikey = (apikey or settings.rousi_apikey or "").strip()
        self.proxy = settings.proxy or None

    @property
    def enabled(self) -> bool:
        return bool(self.apikey)

    # ------------------------------------------------------------------
    @staticmethod
    def _user_agent() -> str:
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.apikey}",
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
            return False, "未配置 Rousi API Key"
        try:
            payload = self._get("/me")
            if payload.get("code") != 0:
                # Key 权限不足时也走这里，提示里带上要授哪些权限
                return False, (
                    f"{payload.get('message', '鉴权失败')}"
                    f"（确认 API Key 已授予「读取账户资料」权限）"
                )
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
        """拼下载地址。Key 放在**路径**里，不是查询参数。

        站点已对该路径关闭访问与错误日志并禁止 Referrer，但地址本身等同于
        凭据，不要外传。

        没有 Key 时退回磁力链只是兜底 —— 私有站没有 DHT，磁力拿不到
        metadata，实际能否用取决于下载器自己连不连得上 tracker。
        """
        if torrent_id and self.apikey:
            return f"{self.host}/api/torrent/download/{self.apikey}/{torrent_id}"
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
