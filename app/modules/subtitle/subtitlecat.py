"""subtitlecat 解析。

流程分两步：搜索页拿详情页地址，详情页里挑简体中文的下载链接。

站点的语言标注不可信 —— 它的中文条目大量是机翻，繁简混杂，还有标着
Chinese 实为日文原文的。所以标注只用来缩小候选范围，最终按正文内容
判定简体（见 base.is_simplified_chinese），判不出就换下一个候选。
"""
from __future__ import annotations

import re

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import SiteClient, absolute_url, text_contains_code
from app.modules.subtitle.base import (
    MAX_SUBTITLE_BYTES,
    MIN_SUBTITLE_BYTES,
    SubtitleItem,
    decode_subtitle,
    is_simplified_chinese,
    looks_like_subtitle,
    pick_suffix,
)

HOST = "https://www.subtitlecat.com"

# 详情页里一个语言块的链接文本，形如「Download Chinese(Simplified) subtitle」。
# 站点对简中的写法有好几种，全都收进来 —— 判定简体最终靠正文，
# 这里放宽只是为了不漏掉候选
_ZH_HINT = re.compile(
    r"chinese|中文|简体|簡體|zh[-_]?(cn|hans)?", re.IGNORECASE
)

# 明确是繁体或粤语的标注，直接排除，省一次下载
_ZH_EXCLUDE = re.compile(
    r"traditional|繁體|繁体|cantonese|zh[-_]?(tw|hk|hant)", re.IGNORECASE
)

# 一个番号最多试几个候选链接。站点上同一部片常挂十几种语言，
# 全试一遍既慢又没必要
MAX_CANDIDATES = 6


class SubtitleCat:
    name = "subtitlecat"

    def __init__(self, host: str = ""):
        if not host:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(host or HOST, interval=2.0)

    def search(self, code: str) -> SubtitleItem | None:
        detail_path, title = self._find_detail(code)
        if not detail_path:
            return None

        html = self.client.get(detail_path)
        if not html:
            return None

        for url in self._candidate_links(html)[:MAX_CANDIDATES]:
            content = self._download(url)
            if not content:
                continue
            if not is_simplified_chinese(content):
                logger.debug(f"[字幕] {code} 候选非简体，跳过: {url}")
                continue
            return SubtitleItem(
                code=code,
                title=title,
                site=self.name,
                content=content,
                suffix=pick_suffix(url),
            )
        return None

    def _find_detail(self, code: str) -> tuple[str, str]:
        """搜索番号，返回 (详情页地址, 标题)。没命中返回 ("", "")。"""
        html = self.client.get("/index.php", params={"search": code})
        if not html:
            return "", ""

        doc = PyQuery(html)
        for row in doc("table tbody tr").items():
            link = row("td a").eq(0)
            href = (link.attr("href") or "").strip()
            title = link.text().strip()
            if not href:
                continue
            # 搜索是模糊匹配，SSIS-001 会带回 SSIS-0011。按番号逐词比对，
            # 避免抓到隔壁那部片的字幕
            if not text_contains_code(title, code):
                continue
            return absolute_url(href, self.client.host), title

        return "", ""

    def _candidate_links(self, html: str) -> list[str]:
        """详情页里可能是简中的下载链接，按可信度排序。"""
        doc = PyQuery(html)
        preferred: list[str] = []
        fallback: list[str] = []

        for link in doc("a").items():
            href = (link.attr("href") or "").strip()
            if not href or not href.lower().endswith(
                (".srt", ".ass", ".ssa", ".vtt")
            ):
                continue

            # 语言标注在链接文本上，也可能在所属区块的标题里
            label = f"{link.text()} {link.parent().text()}"[:200]
            if _ZH_EXCLUDE.search(label):
                continue

            url = absolute_url(href, self.client.host)
            if _ZH_HINT.search(label):
                preferred.append(url)
            else:
                fallback.append(url)

        # 标注像简中的排前面；其余作兜底 —— 站点上有些条目压根没有语言
        # 标注，但正文确实是简体
        seen: set[str] = set()
        return [u for u in preferred + fallback if not (u in seen or seen.add(u))]

    def _download(self, url: str) -> str:
        """下载字幕正文。拿不到或内容不像字幕时返回空串。

        不能复用 SiteClient.get：它返回 str，而字幕编码需要按字节猜
        （站点常给 GBK/BIG5，httpx 会按响应头当 UTF-8 解出乱码）。
        """
        import httpx

        from app.core.config import get_settings

        settings = get_settings()
        try:
            with httpx.Client(
                timeout=20.0,
                follow_redirects=True,
                proxy=settings.proxy or None,
                verify=False,
                trust_env=False,
            ) as client:
                response = client.get(url, headers=self.client.headers())
                response.raise_for_status()
                raw = response.content
        except Exception as exc:
            logger.debug(f"[字幕] 下载失败 {url}: {exc}")
            return ""

        if not MIN_SUBTITLE_BYTES <= len(raw) <= MAX_SUBTITLE_BYTES:
            return ""

        text = decode_subtitle(raw)
        return text if looks_like_subtitle(text) else ""
