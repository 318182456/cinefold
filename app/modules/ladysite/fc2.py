"""FC2 个人拍摄作品解析（官方站 + fc2hub 镜像）。

FC2-PPV 类番号在 javbus/javdb 上基本查不到，这两个源是唯一覆盖。

番号与 URL 的对应：get_true_code 会把各种写法（fc2ppv-1234567、
FC2_PPV_1234567、FC2-1234567）统一成 FC2-PPV-1234567，取末段数字即
官方站的 article id。

官方站（adult.contents.fc2.com）字段少但准确，只有标题、卖家、发布日、
标签，没有片商/系列这些概念 —— 个人投稿本来就没有。
fc2hub 类站点会补上演员名，但那多半是站方猜的，可信度低，因此只在
官方站抓不到时用。
"""
from __future__ import annotations

import re

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import (
    CodeInfo, SiteClient, absolute_url, join_list, normalize_date,
)
from app.utils import get_true_code

HOST = "https://adult.contents.fc2.com"
HUB_HOST = "https://javten.com"

# 从 FC2-PPV-1234567 里取 article id
ARTICLE_RE = re.compile(r"(\d{5,})")

# 站点在标题里加的推广后缀
TITLE_SUFFIXES = ("- FC2-PPV", "FC2-PPV", "- 動画", "無料")



def _article_id(code: str) -> str:
    """FC2-PPV-1234567 → 1234567。取不出返回空串。"""
    match = ARTICLE_RE.search(code or "")
    return match.group(1) if match else ""


# 日期归一化收敛到 base.normalize_date，这里留别名兼容既有调用与测试
_normalize_date = normalize_date


class Fc2:
    """FC2 官方站。字段少但准确。"""

    name = "fc2"

    def __init__(self, host: str = "", cookie: str = ""):
        if not host and not cookie:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(host or HOST, cookie, interval=2.0)

    def crawler_original(self, code: str) -> CodeInfo | None:
        normalized = get_true_code(code)
        # 非 FC2 番号直接放弃，官方站只收录自家投稿
        if not normalized.startswith("FC2"):
            return None

        article = _article_id(normalized)
        if not article:
            return None

        html = self.client.get(f"/article/{article}/")
        if not html:
            return None
        return html_to_code(html, normalized, host=self.client.host)


class Fc2Hub:
    """fc2hub 镜像站。字段比官方站全（带演员），但演员多为站方推测。"""

    name = "fc2hub"

    def __init__(self, host: str = "", cookie: str = ""):
        if not host and not cookie:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(host or HUB_HOST, cookie, interval=2.0, bypass_first=True)

    def crawler_original(self, code: str) -> CodeInfo | None:
        normalized = get_true_code(code)
        if not normalized.startswith("FC2"):
            return None

        article = _article_id(normalized)
        if not article:
            return None

        html = self.client.get(f"/video/{article}")
        if not html:
            return None
        return hub_html_to_code(html, normalized, host=self.client.host)


def html_to_code(html: str, code: str = "", host: str = HOST) -> CodeInfo | None:
    """解析 FC2 官方站详情页。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] fc2 解析失败: {exc}")
        return None

    info = CodeInfo(code=get_true_code(code))

    heading = (
        doc(".items_article_headerInfo h3").eq(0).text()
        or doc("h3").eq(0).text()
        or doc('meta[property="og:title"]').attr("content")
        or ""
    ).strip()
    for suffix in TITLE_SUFFIXES:
        if heading.endswith(suffix):
            heading = heading[: -len(suffix)].strip(" -")
    info.title = heading

    # 卖家即投稿者，FC2 没有片商概念，落到 producer 上最贴近
    seller = (doc(".items_article_headerInfo .items_article_MainitemThumb a").text()
              or doc('a[href*="/users/"]').eq(0).text() or "").strip()
    if seller:
        info.producer = seller

    # 标签就是类别
    tags = [a.text() for a in doc(".tag a, .items_article_TagArea a").items()]
    if tags:
        info.genres = join_list(tags)

    # 販売日在 .items_article_Releasedate 里
    date_text = (
        doc(".items_article_Releasedate").text()
        or doc(".items_article_headerInfo").text()
        or doc("body").text()
    )
    info.release_date = _normalize_date(date_text)

    cover = (
        doc('meta[property="og:image"]').attr("content")
        or doc(".items_article_MainitemThumb img").attr("src")
        or ""
    )
    if cover:
        info.banner = absolute_url(cover, host)
        info.poster = info.banner

    stills = [
        absolute_url(node.attr("href") or node.attr("src") or "", host)
        for node in doc(".items_article_SampleImages a, #media_player img").items()
    ]
    if stills:
        info.still_photo = join_list(stills)

    if not info.code:
        info.code = get_true_code(code)

    # 作品下架时站点返回 200 的提示页，没有发布日
    if not info.release_date:
        logger.debug(f"[{code}] fc2 无此作品或已下架")
        return None
    return info if info.code and info.title else None


def hub_html_to_code(html: str, code: str = "", host: str = HUB_HOST) -> CodeInfo | None:
    """解析 fc2hub 类镜像站详情页。

    结构是 <div class="row"><strong>标签</strong> 值</div> 的松散布局，
    各镜像模板略有差异，因此按整页文本兜底找日期。
    """
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] fc2hub 解析失败: {exc}")
        return None

    info = CodeInfo(code=get_true_code(code))

    heading = (doc("h1").eq(0).text() or doc("h3").eq(0).text() or "").strip()
    if heading:
        prefix = info.code or code
        # 标题前常挂着 FC2-PPV-1234567 前缀
        for candidate in (prefix, prefix.replace("FC2-PPV-", "FC2-"), _article_id(prefix)):
            if candidate and heading.upper().startswith(candidate.upper()):
                heading = heading[len(candidate):].strip(" -：:")
                break
        info.title = heading

    casts = [a.text() for a in doc('a[href*="/actress"], a[href*="/star"]').items()]
    if casts:
        info.casts = join_list(casts)

    tags = [a.text() for a in doc('a[href*="/tag"], a[href*="/genre"]').items()]
    if tags:
        info.genres = join_list(tags)

    info.release_date = _normalize_date(doc("body").text())

    cover = (
        doc('meta[property="og:image"]').attr("content")
        or doc(".video-cover img").attr("src")
        or ""
    )
    if cover:
        info.banner = absolute_url(cover, host)
        info.poster = info.banner

    if not info.code:
        info.code = get_true_code(code)

    if not info.release_date:
        logger.debug(f"[{code}] fc2hub 无此作品")
        return None
    return info if info.code and info.title else None

