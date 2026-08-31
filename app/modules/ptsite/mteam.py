"""M-Team（馒头）。使用官方 API，需在站点后台申请 API Key。"""
from __future__ import annotations

import httpx
from loguru import logger

from app.core.config import get_settings
from app.modules.ptsite import convert_to_mb
from app.schemas.torrent import Torrent
from app.utils.filters import has_chinese, has_uc, has_uhd

API_HOSTS = ["https://api.m-team.cc", "https://api.m-team.io"]


# M-Team 的 discount 取值 → 内部标识。站点用 _2X 前缀表上传翻倍，
# NORMAL 与空值都表示无折扣
DISCOUNT_MAP = {
    "FREE": "free",
    "_2X_FREE": "2x_free",
    "PERCENT_50": "percent_50",
    "_2X_PERCENT_50": "2x_percent_50",
    "PERCENT_30": "percent_30",
    "_2X": "2x",
}


def _normalize_discount(raw) -> str:
    """把站点的 discount 归一化。未知取值原样留着，标注上总比丢掉强。"""
    value = str(raw or "").strip().upper()
    if not value or value == "NORMAL":
        return ""
    return DISCOUNT_MAP.get(value, value.lower().lstrip("_"))


def _is_success(payload: dict) -> bool:
    """M-Team 的 code 有时是字符串 "0"，有时是数字 0。"""
    return str((payload or {}).get("code", "")) == "0"


class MTeam:
    name = "MTeam"

    def __init__(self, api_key: str = ""):
        settings = get_settings()
        self.api_key = api_key or settings.mteam_api_key
        self.proxy = settings.proxy or None
        self._host: str | None = None
        # 最近一次 search 是否失败。search 内部吞异常返回空列表，
        # 调用方靠这个区分"站上没有"和"没问成"（见 crawling_checked）
        self.search_failed = False

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict:
        # 不写死 Content-Type：profile 是无 body 的 POST，
        # 声明成 application/json 会让服务端按空 JSON 解析并报参数错误。
        return {
            "x-api-key": self.api_key,
            "User-Agent": "Mozilla/5.0 cinefold",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=30,
            proxy=self.proxy,
            follow_redirects=True,
            verify=False,
            trust_env=False,
        )

    def _resolve_host(self) -> str:
        """两个域名互为备用，取第一个可用的。"""
        if self._host:
            return self._host
        for host in API_HOSTS:
            try:
                with self._client() as client:
                    response = client.post(
                        f"{host}/api/member/profile", headers=self._headers()
                    )
                if response.status_code < 500:
                    self._host = host
                    return host
            except Exception:
                continue
        self._host = API_HOSTS[0]
        return self._host

    # ------------------------------------------------------------------
    def check_status(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "未配置 M-Team API Key"
        try:
            with self._client() as client:
                response = client.post(
                    f"{self._resolve_host()}/api/member/profile", headers=self._headers()
                )
                response.raise_for_status()
                data = response.json()

            if not _is_success(data):
                return False, data.get("message", "鉴权失败")
            username = (data.get("data") or {}).get("username", "")
            return True, f"连接成功，用户 {username}"
        except Exception as exc:
            return False, str(exc)

    def search(self, keyword: str) -> list[Torrent]:
        self.search_failed = False
        if not self.enabled:
            self.search_failed = True
            return []

        try:
            with self._client() as client:
                response = client.post(
                    f"{self._resolve_host()}/api/torrent/search",
                    headers=self._headers(),
                    json={
                        "keyword": keyword,
                        "mode": "adult",
                        "pageNumber": 1,
                        "pageSize": 50,
                    },
                )
                response.raise_for_status()
                payload = response.json()

            if not _is_success(payload):
                logger.warning(f"[MTeam] 搜索失败: {payload.get('message')}")
                self.search_failed = True
                return []

            items = ((payload.get("data") or {}).get("data")) or []
            return [self._convert(item, keyword) for item in items]
        except Exception as exc:
            logger.warning(f"[MTeam] 搜索异常: {exc}")
            self.search_failed = True
            return []

    def _convert(self, item: dict, code: str) -> Torrent:
        title = f"{item.get('name', '')} {item.get('smallDescr', '')}".strip()
        status = item.get("status") or {}
        # discount 为 FREE / PERCENT_50 / _2X_FREE 等
        discount = _normalize_discount(status.get("discount"))
        # 只有完全不计下载量的才算 free，PERCENT_50 这类不算
        free = discount in ("free", "2x_free")

        return Torrent(
            id=int(item.get("id") or 0),
            site=self.name,
            title=title,
            size_mb=convert_to_mb(f"{int(item.get('size') or 0) / 1024 / 1024} MB"),
            seeders=int(status.get("seeders") or 0),
            chinese=has_chinese(title),
            uc=has_uc(title),
            uhd=has_uhd(title),
            free=free,
            discount=discount,
            download_url="",  # 需要单独换取带 token 的下载链接
            detail_url=f"https://kp.m-team.cc/detail/{item.get('id')}",
            code=code,
        )

    def get_torrent_download_url(self, torrent_id: int | str) -> str:
        """换取带一次性 token 的下载地址。"""
        if not self.enabled:
            return ""
        try:
            with self._client() as client:
                response = client.post(
                    f"{self._resolve_host()}/api/torrent/genDlToken",
                    headers=self._headers(),
                    data={"id": str(torrent_id)},
                )
                response.raise_for_status()
                payload = response.json()

            if not _is_success(payload):
                logger.warning(f"[MTeam] 获取下载链接失败: {payload.get('message')}")
                return ""
            return payload.get("data") or ""
        except Exception as exc:
            logger.warning(f"[MTeam] 获取下载链接异常: {exc}")
            return ""

    def download_seed(self, torrent: Torrent) -> bytes | None:
        url = torrent.download_url or self.get_torrent_download_url(torrent.id)
        if not url:
            return None
        from app.modules.ptsite import download_seed_by_url
        return download_seed_by_url(url, proxy=self.proxy)
