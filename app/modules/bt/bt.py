"""自定义 BT 资源源。

用户通过 BT_URL 配置任意接口，返回 {"data": [Torrent, ...]} 即可接入。
请求方法、请求头、POST body 均可配置，`${keyword}` 会被替换成番号。
"""
from __future__ import annotations

import json

import httpx
from loguru import logger

from app.core.config import get_settings
from app.modules.ptsite import download_seed_by_url
from app.schemas.torrent import Torrent

KEYWORD_PLACEHOLDERS = ("${keyword}", "{keyword}", "%s")


def add_keyword_param(target: str, keyword: str) -> str:
    """把番号填进 URL 或 body 模板。

    模板里没有占位符时，URL 追加 keyword 查询参数，body 原样返回。
    """
    if not target:
        return target

    for placeholder in KEYWORD_PLACEHOLDERS:
        if placeholder in target:
            return target.replace(placeholder, keyword)

    if target.lstrip().startswith(("{", "[")):
        return target

    separator = "&" if "?" in target else "?"
    return f"{target}{separator}keyword={keyword}"


class BT:
    name = "BT"

    # 最近一次 search 是否失败，见 ptsite.crawling_checked
    search_failed = False

    def __init__(self, url: str = "", method: str = "", headers: str = "", body: str = ""):
        settings = get_settings()
        self.url = url or settings.bt_url
        self.method = (method or settings.bt_method or "get").lower()
        self.raw_headers = headers or settings.bt_header
        self.raw_body = body or settings.bt_json_data
        self.proxy = settings.proxy or None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def _headers(self) -> dict:
        base = {"User-Agent": "Mozilla/5.0 cinefold"}
        if not self.raw_headers:
            return base
        try:
            base.update(json.loads(self.raw_headers))
        except json.JSONDecodeError:
            logger.warning("BT_HEADER 不是合法 JSON，已忽略")
        return base

    # ------------------------------------------------------------------
    def search(self, keyword: str) -> list[Torrent]:
        self.search_failed = False
        if not self.enabled:
            self.search_failed = True
            return []

        url = add_keyword_param(self.url, keyword)
        payload = None
        if self.raw_body:
            try:
                payload = json.loads(add_keyword_param(self.raw_body, keyword))
            except json.JSONDecodeError:
                logger.warning("BT_JSON_DATA 不是合法 JSON，已忽略")

        try:
            with httpx.Client(timeout=30, proxy=self.proxy, follow_redirects=True) as client:
                response = client.request(
                    self.method.upper(), url, headers=self._headers(), json=payload
                )
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            logger.warning(f"[BT] 请求失败: {exc}")
            self.search_failed = True
            return []

        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            logger.warning("[BT] 返回格式不符合 {\"data\": [...]} 约定")
            return []

        results: list[Torrent] = []
        for item in items:
            try:
                torrent = Torrent.from_dict(item)
                # site 无条件覆写：过滤、排序、种子反查都按它认站，接口自报的
                # 站名若能覆盖，BT_AUTO_DOWNLOAD 关掉也拦不住这些种子。
                # 原站名挪到 source_site，只用于展示。
                torrent.source_site = torrent.source_site or torrent.site
                torrent.site = self.name
                torrent.code = torrent.code or keyword
                results.append(torrent)
            except Exception as exc:
                logger.debug(f"[BT] 跳过异常条目: {exc}")

        return results

    def download_seed(self, torrent: Torrent) -> bytes | None:
        """磁链交给下载器直接处理，只有 http 种子才需要下载。"""
        if not torrent.download_url or torrent.download_url.startswith("magnet:"):
            return None
        return download_seed_by_url(
            torrent.download_url, headers=self._headers(), proxy=self.proxy
        )

    def check_status(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "未配置 BT_URL"
        return (True, "配置已就绪") if self.search("TEST-000") is not None else (False, "请求失败")
