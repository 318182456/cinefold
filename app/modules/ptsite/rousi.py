"""Rousi（站点后端已改名 PeerGo）。

认证用个人 API Key，凭据只此一项 —— 账号密码登录换 JWT、Tracker Passkey
都已废弃。同一把 Key 也能给 MoviePilot、PT-depiler 等工具用，需在站点
授予「读取账户资料」「读取与搜索种子」「下载种子」三项权限。

接口在 2026 年换了一版，实测确认的差异（旧实现全部对不上）：

- 路径前缀是 /api/v1，旧的 /api/torrent/search 与 /api/me 都已 404
- 搜索走 GET /api/v1/torrents?query=&limit=&offset=
- Key 必须用 `X-API-Key` 头传。用 `Authorization: Bearer` 会被路由到
  一个旧版兼容接口：它照样回 200，但返回 {"code":0,"data":{"torrents"}}
  且**忽略全部查询参数** —— 于是每次搜索都拿回全站第一页 100 条，
  番号搜索静默失效。这个坑不看返回结构发现不了。
- 新接口返回 {"items":[...],"total":N,"limit":,"offset":}，字段名也变了：
  name（原 title）、size_bytes（原 size）、promotion 是字符串（原对象）
- 列表项不含 info_hash，要 GET /api/v1/torrents/{id} 才有 info_hash_v1

下载：GET /api/v1/torrents/{id}/download 只认浏览器会话（401
web_session_required），API Key 用不了。站点说 Key 的下载走上游协议的
专用路径、Key 在路径里，但那条路径未在前端代码中出现，也未验证成功 ——
详见 _build_download_url 的说明。
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
# 一次搜索取多少条。番号搜索通常只有几条，给足冗余即可
SEARCH_LIMIT = 50


class Rousi:
    name = "Rousi"

    # 最近一次 search 是否失败，见 ptsite.crawling_checked
    search_failed = False

    def __init__(self, apikey: str = "", host: str = ""):
        settings = get_settings()
        self.host = (host or os.getenv("ROUSI_HOST", "") or DEFAULT_HOST).rstrip("/")
        # 从网页复制 Key 容易带上首尾空白，原样进 header 会鉴权失败
        self.apikey = (apikey or settings.rousi_apikey or "").strip()
        self.proxy = settings.proxy or None

    @property
    def enabled(self) -> bool:
        return bool(self.apikey)

    # ------------------------------------------------------------------
    def _headers(self) -> dict:
        return {
            # 必须是 X-API-Key。换成 Authorization: Bearer 会落到旧版接口，
            # 那边忽略查询参数，搜索会静默返回全站第一页
            "X-API-Key": self.apikey,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 cinefold",
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
                f"{self.host}/api/v1{path}", headers=self._headers(), params=params
            )
            response.raise_for_status()
            return response.json()

    # ------------------------------------------------------------------
    def check_status(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "未配置 Rousi API Key"
        try:
            # 用一次最小搜索验证：/api/v1/me 已不存在，而 me/ 下的接口要
            # 浏览器会话，API Key 一律 401，拿它判断会误报未配置
            payload = self._get("/torrents", {"limit": 1, "offset": 0})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                return False, "API Key 无效或已撤销"
            if exc.response.status_code == 403:
                return False, "API Key 权限不足，需授予「读取与搜索种子」"
            return False, f"HTTP {exc.response.status_code}"
        except Exception as exc:
            return False, str(exc)[:60]

        if not isinstance(payload, dict) or "items" not in payload:
            # 落到旧接口时是这个形状，说明 Key 没走对头
            return False, "响应结构异常，可能命中了旧版接口"
        return True, f"连接成功，站上共 {payload.get('total', '?')} 个种子"

    # ------------------------------------------------------------------
    def search(self, keyword: str) -> list[Torrent]:
        self.search_failed = False
        if not self.enabled:
            self.search_failed = True
            return []

        code = get_true_code(keyword) or keyword
        try:
            payload = self._get(
                "/torrents",
                {"query": code, "limit": SEARCH_LIMIT, "offset": 0},
            )
        except Exception as exc:
            logger.warning(f"[Rousi] 搜索异常: {exc}")
            self.search_failed = True
            return []

        items = payload.get("items")
        if items is None:
            # 旧接口的形状。真发生了就是 Key 传法不对，搜索结果不可信，
            # 当失败处理而不是把全站第一页当成命中
            logger.warning(
                f"[Rousi] 搜索响应缺少 items，疑似命中旧版接口: "
                f"{list(payload)[:5]}"
            )
            self.search_failed = True
            return []

        return [self._convert(item, code) for item in items]

    def _convert(self, item: dict, code: str) -> Torrent:
        # 新接口的字段是 name / size_bytes，旧的是 title / size
        title = str(item.get("name") or item.get("title") or "")
        subtitle = str(item.get("subtitle") or "")
        # 中文判定要带上副标题：站上主标题常是纯日文原名，中文信息在副标题里
        combined = f"{title} {subtitle}"

        # promotion 现在是字符串：none / free / double_upload_free …
        promotion = str(item.get("promotion") or "").lower()

        return Torrent(
            id=int(item.get("id") or 0),
            site=self.name,
            title=title,
            size_mb=round(int(item.get("size_bytes") or 0) / 1024 / 1024, 2),
            seeders=int(item.get("seeders") or 0),
            chinese=has_chinese(combined),
            uc=has_uc(combined),
            uhd=has_uhd(title),
            free="free" in promotion,
            download_url=self._build_download_url(item.get("id")),
            detail_url=f"{self.host}/torrents/{item.get('id')}",
            code=code,
        )

    def _build_download_url(self, torrent_id) -> str:
        """下载地址。

        /api/v1/torrents/{id}/download 存在但只认浏览器会话（401
        web_session_required），API Key 无论放 header 还是查询参数都过不去。

        站点文档说 Key 的下载走上游协议的专用路径、Key 在请求路径里，但
        实测 /api/torrent/download/{key}/{id} 是 404，前端代码里也没有这条
        路径（网页自己走会话下载，不会引用它）。真实形式未确认。

        所以这里仍拼 /api/v1 那条并带上 Key：拿到正确路径后只改这一处即可。
        下载失败会在 download_seed 里报出来，不会静默当成成功。

        这个地址等同于凭据，别外传，日志里也不打印完整地址。
        """
        if not torrent_id or not self.apikey:
            return ""
        return f"{self.host}/api/v1/torrents/{torrent_id}/download"

    def download_seed(self, torrent: Torrent) -> bytes | None:
        url = torrent.download_url
        if not url or url.startswith("magnet:"):
            return None
        return download_seed_by_url(
            url, headers=self._headers(), proxy=self.proxy
        )
