"""NexusPHP 系站点的通用抓取逻辑。

rousi / ptt / nicept 都是 NexusPHP 架构，搜索页结构基本一致，
差异集中在域名、免费标识的 class 名与列位置，由子类通过类属性覆盖。
"""
from __future__ import annotations

import os
import re
from urllib.parse import urljoin

import httpx
from loguru import logger
from pyquery import PyQuery

from app.core.config import get_settings
from app.modules.ptsite import convert_to_mb, download_seed_by_url
from app.schemas.torrent import Torrent
from app.utils.filters import has_chinese, has_uc, has_uhd

# free 的标识在不同站点分别用图片 alt、span class 或背景色表示
FREE_MARKERS = ("free", "免费", "pro_free")


class NexusSite:
    """子类需要覆盖 name / host / cookie。"""
    name = "Nexus"
    host = ""
    search_path = "/torrents.php"

    def __init__(self, cookie: str = "", host: str = ""):
        settings = get_settings()
        self.cookie = cookie
        self.proxy = settings.proxy or None
        # PT 站换域名较频繁，允许用 <NAME>_HOST 环境变量覆盖
        override = host or os.getenv(f"{self.name.upper()}_HOST", "")
        if override:
            self.host = override.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.cookie and self.host)

    def _headers(self) -> dict:
        return {
            "Cookie": self.cookie,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
        }

    # ------------------------------------------------------------------
    def search(self, keyword: str) -> list[Torrent]:
        if not self.enabled:
            return []

        try:
            with httpx.Client(
                timeout=30,
                proxy=self.proxy,
                follow_redirects=True,
                # PT 站常用自签或链路不全的证书，且只认配置里的代理
                verify=False,
                trust_env=False,
            ) as client:
                response = client.get(
                    urljoin(self.host, self.search_path),
                    headers=self._headers(),
                    params={"search": keyword, "search_mode": 0},
                )
                response.raise_for_status()
                html = response.text
        except Exception as exc:
            logger.warning(f"[{self.name}] 搜索请求失败: {exc}")
            return []

        # Cookie 失效时会被重定向到登录页
        if "takelogin.php" in html or "登录" in html[:500]:
            logger.warning(f"[{self.name}] Cookie 可能已失效")
            return []

        # 站点换域名后原地址只剩 JS 跳转页，页面很小且无种子表
        if len(html) < 8000 and "torrents" not in html and "details.php" not in html:
            logger.warning(
                f"[{self.name}] {self.host} 返回的不是种子列表页，"
                f"站点可能已更换域名，请更新配置"
            )
            return []

        return self._parse(html, keyword)

    def _parse(self, html: str, code: str) -> list[Torrent]:
        results: list[Torrent] = []
        try:
            doc = PyQuery(html)
            rows = doc("table.torrents > tr, table.torrents > tbody > tr")

            for row in rows.items():
                link = row("a[href*='details.php']").eq(0)
                if not link:
                    continue

                title = (link.attr("title") or link.text() or "").strip()
                if not title:
                    continue

                detail_href = link.attr("href") or ""
                torrent_id = self._extract_id(detail_href)

                download_href = row("a[href*='download.php']").eq(0).attr("href") or ""
                download_url = urljoin(self.host, download_href) if download_href else ""

                results.append(Torrent(
                    id=torrent_id,
                    site=self.name,
                    title=title,
                    size_mb=self._extract_size(row),
                    seeders=self._extract_seeders(row),
                    chinese=has_chinese(title),
                    uc=has_uc(title),
                    uhd=has_uhd(title),
                    free=self._is_free(row),
                    download_url=download_url,
                    detail_url=urljoin(self.host, detail_href),
                    code=code,
                ))
        except Exception as exc:
            logger.warning(f"[{self.name}] 解析页面失败: {exc}")

        return results

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_id(href: str) -> int:
        match = re.search(r"id=(\d+)", href or "")
        return int(match.group(1)) if match else 0

    @staticmethod
    def _extract_size(row) -> float:
        """体积列没有固定 class，扫所有单元格找带单位的那个。"""
        for cell in row("td").items():
            text = cell.text() or ""
            if re.search(r"[\d.]+\s*(TB|GB|MB)", text, re.IGNORECASE):
                return convert_to_mb(text)
        return 0.0

    @staticmethod
    def _extract_seeders(row) -> int:
        """做种数通常是 seeders.php 链接，取不到时退回倒数第三列。"""
        seeder_link = row("a[href*='seeders']").eq(0)
        if seeder_link:
            digits = re.sub(r"\D", "", seeder_link.text() or "")
            if digits:
                return int(digits)

        cells = list(row("td").items())
        if len(cells) >= 3:
            digits = re.sub(r"\D", "", cells[-3].text() or "")
            if digits.isdigit():
                return int(digits)
        return 0

    @staticmethod
    def _is_free(row) -> bool:
        html = (row.html() or "").lower()
        return any(marker in html for marker in FREE_MARKERS)

    # ------------------------------------------------------------------
    def download_seed(self, torrent: Torrent) -> bytes | None:
        if not torrent.download_url:
            return None
        return download_seed_by_url(
            torrent.download_url, cookie=self.cookie, proxy=self.proxy
        )

    def check_status(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, f"未配置 {self.name} Cookie"
        try:
            with httpx.Client(
                timeout=20,
                proxy=self.proxy,
                follow_redirects=True,
                verify=False,
                trust_env=False,
            ) as client:
                response = client.get(urljoin(self.host, "/index.php"), headers=self._headers())
                response.raise_for_status()
            if "takelogin.php" in response.text:
                return False, "Cookie 已失效"
            return True, "连接成功"
        except Exception as exc:
            return False, str(exc)
