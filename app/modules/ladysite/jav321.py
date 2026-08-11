"""jav321 解析。

详情页 URL 可由番号推算：SSIS-001 → /video/ssis00001（数字补到 5 位），
省掉一次搜索请求。信息区是 `<b>标签</b>: 值<br>` 的平铺结构，没有表格。
封面取自 dmm 的图床，带预览视频与剧照。
"""
from __future__ import annotations

import re

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import CodeInfo, SiteClient, join_list
from app.utils import get_true_code

HOST = "https://www.jav321.com"

FIELD_MAP = {
    "出演者": "casts",
    "メーカー": "producer",
    "レーベル": "publisher",
    "シリーズ": "series",
    "配信開始日": "release_date",
    "発売日": "release_date",
    "収録時間": "duration",
}

# 评分形如「平均評価: 4.5」
STAR_RE = re.compile(r"平均評価[^0-9]*([0-9.]+)")


class Jav321:
    name = "jav321"

    def __init__(self, host: str = ""):
        if not host:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(host or HOST, interval=1.5)

    def crawler_original(self, code: str) -> CodeInfo | None:
        normalized = get_true_code(code)
        path = _detail_path(normalized)
        if not path:
            return None

        html = self.client.get(path)
        if not html:
            return None
        return html_to_code(html, normalized)


def _detail_path(code: str) -> str:
    """SSIS-001 → /video/ssis00001。数字段补零到 5 位。"""
    if "-" not in (code or ""):
        return ""
    prefix, _, number = code.rpartition("-")
    if not prefix or not number.isdigit():
        return ""
    return f"/video/{prefix.lower().replace('-', '')}{int(number):05d}"


def html_to_code(html: str, code: str = "") -> CodeInfo | None:
    """解析 jav321 详情页。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] jav321 解析失败: {exc}")
        return None

    info = CodeInfo(code=get_true_code(code))

    body = doc(".panel-body").eq(0)
    if not body:
        body = doc("body")

    # <b>标签</b>: 值<br> —— 取每个 <b> 到下一个 <br> 之间的内容
    raw = body.html() or ""
    for label, field in FIELD_MAP.items():
        match = re.search(
            rf"<b>\s*{re.escape(label)}\s*</b>\s*[:：]?(.*?)(?:<br\s*/?>|<b>)",
            raw,
            re.S,
        )
        if not match:
            continue

        chunk = PyQuery(f"<div>{match.group(1)}</div>")
        if field in ("casts", "genres"):
            value = join_list(a.text() for a in chunk("a").items())
        else:
            value = (chunk.text() or "").strip()

        if value and not getattr(info, field, ""):
            setattr(info, field, value)

    # 标题在 h3 里，<small> 是番号与演员的重复信息
    heading = doc(".panel-heading h3").eq(0)
    if heading:
        heading.find("small").remove()
        info.title = (heading.text() or "").strip()

    star = STAR_RE.search(doc.text() or "")
    if star:
        try:
            info.star = float(star.group(1))
        except ValueError:
            pass

    # ps 是小图、pl 是大图，优先用大图
    cover = ""
    for img in doc("img").items():
        src = img.attr("src") or ""
        if "ps.jpg" in src or "pl.jpg" in src:
            cover = src.replace("ps.jpg", "pl.jpg")
            break
    if cover:
        info.banner = cover
        info.poster = cover

    # 剧照散在整页的画廊里，不在信息区
    stills = [
        src for img in doc("img").items()
        # 同一张图会有 dmm 与站内镜像两份，join_list 去重
        if "jp-" in (src := img.attr("src") or "")
    ]
    if stills:
        info.still_photo = join_list(stills)

    preview = doc("video source").attr("src") or ""
    if preview:
        info.preview_url = preview

    if not info.code:
        info.code = get_true_code(code)
    return info if info.code and info.title else None
