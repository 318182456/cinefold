"""Hbox 解析。

日本作品的收录站，详情页地址可由番号推算（/works/<番号>），
不必先搜索，这点与 javbus 一样。字段偏少，主要价值是作为
主源全挂时的补充。

信息区是 <dl><dt>标签</dt><dd>值</dd></dl>，与 carib 同构。
"""
from __future__ import annotations

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import (
    CodeInfo, SiteClient, absolute_url, join_list, normalize_date,
)
from app.utils import get_true_code

HOST = "https://hbox.jp"

FIELD_MAP = {
    "出演": "casts",
    "出演者": "casts",
    "発売日": "release_date",
    "配信開始日": "release_date",
    "収録時間": "duration",
    "メーカー": "producer",
    "レーベル": "publisher",
    "シリーズ": "series",
    "ジャンル": "genres",
    "タグ": "genres",
}


class Hbox:
    name = "hbox"

    def __init__(self, host: str = "", cookie: str = ""):
        if not host and not cookie:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(host or HOST, cookie, interval=2.0)

    def crawler_original(self, code: str) -> CodeInfo | None:
        normalized = get_true_code(code)
        if not normalized:
            return None

        html = self.client.get(f"/works/{normalized.lower()}")
        if not html:
            return None
        return html_to_code(html, normalized, host=self.client.host)


def html_to_code(html: str, code: str = "", host: str = HOST) -> CodeInfo | None:
    """解析 Hbox 详情页。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] hbox 解析失败: {exc}")
        return None

    info = CodeInfo(code=get_true_code(code))

    # dt 与 dd 按顺序配对
    for block in doc("dl").items():
        labels = list(block("dt").items())
        values = list(block("dd").items())
        for label_node, value_node in zip(labels, values):
            label = (label_node.text() or "").strip().rstrip("：: ")
            field = FIELD_MAP.get(label)
            if not field:
                continue

            if field in ("casts", "genres"):
                links = [a.text() for a in value_node("a").items()]
                value = join_list(links) if links else (value_node.text() or "").strip()
            else:
                value = (value_node.text() or "").strip()
            if value and value.strip("-") and not getattr(info, field, ""):
                setattr(info, field, value)

    if info.release_date:
        info.release_date = normalize_date(info.release_date) or info.release_date

    heading = (doc("h1").eq(0).text() or doc(".work-title").eq(0).text() or "").strip()
    if heading:
        prefix = info.code or code
        if prefix and heading.upper().startswith(prefix.upper()):
            heading = heading[len(prefix):].strip(" -：:")
        info.title = heading

    cover = (
        doc('meta[property="og:image"]').attr("content")
        or doc(".work-image img").attr("src")
        or ""
    )
    if cover:
        info.banner = absolute_url(cover, host)
        info.poster = info.banner

    stills = [
        absolute_url(node.attr("href") or node.attr("src") or "", host)
        for node in doc(".sample-images a, .gallery a").items()
    ]
    if stills:
        info.still_photo = join_list(stills)

    if not info.code:
        info.code = get_true_code(code)

    # 番号不存在时站点返回 200 的提示页，没有发售日
    if not info.release_date:
        logger.debug(f"[{code}] hbox 无此作品")
        return None
    return info if info.code and info.title else None

