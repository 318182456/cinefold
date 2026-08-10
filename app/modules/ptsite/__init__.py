"""PT 站点公共逻辑与搜索聚合。"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Protocol, Sequence, runtime_checkable

import httpx
from loguru import logger

from app.core.config import get_settings
from app.schemas.torrent import Torrent
from app.utils.filters import has_chinese, has_uc, has_uhd

# 前端搜索接口的整体超时，超过后返回已就绪站点的结果
SEARCH_TIMEOUT_SECONDS = 45.0

SIZE_RE = re.compile(r"([\d.]+)\s*(TB|GB|MB|KB|T|G|M|K)", re.IGNORECASE)
UNIT_TO_MB = {"TB": 1024 * 1024, "T": 1024 * 1024, "GB": 1024, "G": 1024,
              "MB": 1, "M": 1, "KB": 1 / 1024, "K": 1 / 1024}


@runtime_checkable
class PTSite(Protocol):
    name: str
    enabled: bool
    def search(self, keyword: str) -> list[Torrent]: ...
    def download_seed(self, torrent: Torrent) -> bytes | None: ...


def convert_to_mb(size_text: str) -> float:
    """"1.5 GB" → 1536.0"""
    if not size_text:
        return 0.0
    match = SIZE_RE.search(str(size_text))
    if not match:
        try:
            return float(size_text)
        except (TypeError, ValueError):
            return 0.0
    return round(float(match.group(1)) * UNIT_TO_MB.get(match.group(2).upper(), 1), 2)


def extract_torrent_size(text: str) -> float:
    return convert_to_mb(text)


def convert_torrent(raw: dict, site: str, code: str = "") -> Torrent:
    """把站点原始数据转成统一 Torrent。

    属性字段站点常常不提供，用标题推断兜底。
    """
    title = str(raw.get("title") or raw.get("name") or "")
    return Torrent(
        id=int(raw.get("id") or 0),
        site=site,
        title=title,
        size_mb=convert_to_mb(raw.get("size") or raw.get("size_mb") or 0),
        seeders=int(raw.get("seeders") or raw.get("seeder") or 0),
        chinese=bool(raw.get("chinese")) or has_chinese(title),
        uc=bool(raw.get("uc")) or has_uc(title),
        uhd=bool(raw.get("uhd")) or has_uhd(title),
        free=bool(raw.get("free")),
        download_url=str(raw.get("download_url") or ""),
        detail_url=str(raw.get("detail_url") or ""),
        code=code,
    )


def download_seed_by_url(
    url: str, cookie: str = "", headers: dict | None = None, proxy: str | None = None
) -> bytes | None:
    """下载 .torrent 文件。"""
    if not url:
        return None

    request_headers = {"User-Agent": "Mozilla/5.0 byte-muse"}
    if cookie:
        request_headers["Cookie"] = cookie
    request_headers.update(headers or {})

    try:
        with httpx.Client(
            timeout=60,
            follow_redirects=True,
            proxy=proxy,
            verify=False,
            trust_env=False,
        ) as client:
            response = client.get(url, headers=request_headers)
            response.raise_for_status()
            content = response.content

        # 站点异常时会返回 HTML 错误页，用 bencode 头判断
        if not content.startswith(b"d"):
            logger.warning(f"返回内容不是种子文件: {url}")
            return None
        return content
    except Exception as exc:
        logger.error(f"下载种子失败 {url}: {exc}")
        return None


# ----------------------------------------------------------------------
def get_sites() -> list[PTSite]:
    """返回所有已配置的 PT 站。"""
    settings = get_settings()
    sites: list[PTSite] = []

    if settings.mteam_api_key:
        from app.modules.ptsite.mteam import MTeam
        sites.append(MTeam())
    if settings.rousi_cookie:
        from app.modules.ptsite.rousi import Rousi
        sites.append(Rousi())
    if settings.ptt_cookie:
        from app.modules.ptsite.ptt import PTT
        sites.append(PTT())
    if settings.nicept_cookie:
        from app.modules.ptsite.nicept import NicePT
        sites.append(NicePT())
    if settings.bt_url:
        from app.modules.bt.bt import BT
        sites.append(BT())

    return sites


def crawling(site: PTSite, keyword: str) -> list[Torrent]:
    """单站搜索，异常不外抛。"""
    try:
        return site.search(keyword) or []
    except Exception as exc:
        logger.warning(f"[{site.name}] 搜索 {keyword} 失败: {exc}")
        return []


def search_pt(
    keyword: str,
    sites: Sequence[PTSite] | None = None,
    timeout: float = SEARCH_TIMEOUT_SECONDS,
) -> list[Torrent]:
    """并发搜索所有站点，合并结果。

    单站点卡住不应拖垮整体，超时后返回已完成站点的结果。
    """
    targets = list(sites) if sites is not None else get_sites()
    if not targets:
        logger.warning("未配置任何 PT 站点")
        return []

    results: list[Torrent] = []
    finished = 0
    pool = ThreadPoolExecutor(max_workers=min(len(targets), 8))
    try:
        futures = {pool.submit(crawling, site, keyword): site for site in targets}
        try:
            for future in as_completed(futures, timeout=timeout):
                results.extend(future.result())
                finished += 1
        except FuturesTimeout:
            slow = [
                site.name for future, site in futures.items() if not future.done()
            ]
            logger.warning(f"[{keyword}] 搜索超时，未完成的站点: {', '.join(slow)}")
    finally:
        # 不等待未完成的任务，避免请求线程被阻塞
        pool.shutdown(wait=False, cancel_futures=True)

    logger.info(
        f"[{keyword}] 共搜到 {len(results)} 个种子，"
        f"{finished}/{len(targets)} 个站点返回"
    )
    return results


def get_site_by_name(name: str) -> PTSite | None:
    for site in get_sites():
        if site.name.lower() == (name or "").lower():
            return site
    return None
