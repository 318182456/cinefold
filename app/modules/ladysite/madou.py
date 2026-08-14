"""Madou / Madouqu 解析。

国产（麻豆、天美、蜜桃等）作品的收录站。这类番号（MDX-0123、MD-0180、
TM-0092）在日系源上完全查不到，只能靠这两个站。

两站都是 WordPress 模板：标题在 h1，元信息散在 .entry-meta / .tags 里，
没有 javbus 那种规整的信息表。字段能拿到的比日系源少 —— 国产片本身
就没有片商/系列/时长这套元数据，多数只有标题、日期、演员、标签。

搜索必须先做：详情页是 /?p=123 或 /文章别名/ 这类不可推算的地址。
"""
from __future__ import annotations

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import (
    CodeInfo, SiteClient, join_list, normalize_date, text_contains_code,
)
from app.utils import get_true_code

MADOU_HOST = "https://madou.club"
MADOUQU_HOST = "https://madouqu.com"



class _MadouBase:
    """两站共用逻辑，只有 host 与是否需要过盾不同。"""

    name = ""
    default_host = ""
    default_bypass = False

    def __init__(self, host: str = "", cookie: str = ""):
        if not host and not cookie:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(
            host or self.default_host,
            cookie,
            interval=2.0,
            bypass_first=self.default_bypass,
        )

    def crawler_original(self, code: str) -> CodeInfo | None:
        normalized = get_true_code(code)
        if not normalized:
            return None

        detail_url = self.search_detail_url(normalized)
        if not detail_url:
            return None

        html = self.client.get(detail_url)
        if not html:
            return None
        return html_to_code(html, normalized, self.name)

    def search_detail_url(self, code: str) -> str:
        """WordPress 的搜索是 /?s=关键词。"""
        html = self.client.get("/", params={"s": code})
        return html_to_detail_url(html, code, self.name) if html else ""


class Madou(_MadouBase):
    name = "madou"
    default_host = MADOU_HOST


class Madouqu(_MadouBase):
    name = "madouqu"
    default_host = MADOUQU_HOST
    default_bypass = True


def html_to_detail_url(html: str, code: str = "", site: str = "madou") -> str:
    """搜索结果里找标题含目标番号的那条。

    国产站标题写法很乱（MDX-0123 / MDX0123 / mdx-0123 混用），
    因此比对时把标题里的番号也过一遍 get_true_code。
    """
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] {site} 解析搜索页失败: {exc}")
        return ""

    target = get_true_code(code)
    if not target:
        return ""

    # 文章列表项通常是 article 或 .post
    for item in doc("article, .post, .entry").items():
        link = item("h2 a, h1 a, .entry-title a, a").eq(0)
        href = link.attr("href") or ""
        if not href:
            continue
        title = (link.attr("title") or link.text() or "").strip()
        if _title_matches(title, target):
            return href

    # 模板不匹配时退回全页链接扫描
    for link in doc("a").items():
        href = link.attr("href") or ""
        title = (link.attr("title") or link.text() or "").strip()
        if href and _title_matches(title, target):
            return href
    return ""


# 整词比对（见 base.text_contains_code）：单纯的拍平子串包含会让 MD-0180
# 命中多部曲的 "MD-0180-1"，把别集的数据写进目标番号
_title_matches = text_contains_code


def html_to_code(html: str, code: str = "", site: str = "madou") -> CodeInfo | None:
    """解析国产站详情页。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] {site} 解析失败: {exc}")
        return None

    info = CodeInfo(code=get_true_code(code))

    raw_heading = (
        doc("h1.entry-title").eq(0).text()
        or doc("h1").eq(0).text()
        or doc('meta[property="og:title"]').attr("content")
        or ""
    ).strip()
    heading = raw_heading
    if heading:
        # 标题常以番号开头，剥掉后才是片名
        prefix = info.code or code
        for candidate in (prefix, prefix.replace("-", "")):
            if candidate and heading.upper().startswith(candidate.upper()):
                heading = heading[len(candidate):].strip(" -：:")
                break
        info.title = heading

    # 演员与标签都在分类链接里，靠 URL 段区分
    info.casts = join_list(
        a.text() for a in doc('a[rel="tag"], .tags a').items()
        if "/actor" in (a.attr("href") or "") or "/star" in (a.attr("href") or "")
    )
    tags = [
        a.text() for a in doc('a[rel="tag"], .tags a, .entry-categories a').items()
        if "/actor" not in (a.attr("href") or "") and "/star" not in (a.attr("href") or "")
    ]
    if tags:
        info.genres = join_list(tags)

    # 日期在 <time> 上，或元信息区的文本里
    date_text = (
        doc("time").attr("datetime")
        or doc("time").eq(0).text()
        or doc(".entry-meta, .entry-date, .post-meta").eq(0).text()
        or ""
    )
    info.release_date = normalize_date(date_text)

    cover = (
        doc('meta[property="og:image"]').attr("content")
        or doc(".entry-content img").attr("src")
        or doc("article img").attr("src")
        or ""
    )
    if cover:
        info.banner = cover
        info.poster = cover

    if not info.code:
        info.code = get_true_code(code)

    # 国产站没有发行日期这类硬字段可做存在性校验，只能靠标题：
    # 搜不到时 WordPress 返回的是"没有找到"页，h1 是提示语而非片名。
    # 因此要求原始标题（剥番号前缀之前的那个）里确实带着目标番号
    if not _title_matches(raw_heading, info.code):
        logger.debug(f"[{code}] {site} 无此番号")
        return None
    return info if info.code and info.title else None
