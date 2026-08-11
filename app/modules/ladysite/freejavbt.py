"""freejavbt 解析。

详情页就是 /zh/<番号>。信息区是 div.single-video-meta，
每块形如 <span>标签:</span> 后跟纯文本或链接。

注意：实测这个站的数据会串号，不可作为详情主源。
SSIS-001 的标题给的是另一部作品（写三上悠亞，实际是葵つかさ），
MIDE-777 的演员也对不上（写水上由纪恵，实际是高橋しょう子）。
因此没有加入 DETAIL_SITES，只在数据源页登记、可测连通，
需要时由用户显式启用。
"""
from __future__ import annotations

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import CodeInfo, SiteClient, join_list
from app.utils import get_true_code

HOST = "https://freejavbt.com"

# 标签 → 字段。「女优」里混着男优，单独处理。
# 「导演」不映射到 producer——那是片商字段，映射过去会把导演名当片商
FIELD_MAP = {
    "日期": "release_date",
    "时长": "duration",
    "系列": "series",
    "片商": "publisher",
    "类别": "genres",
    "類別": "genres",
}

# 站点在标题末尾追加的推广后缀
TITLE_SUFFIXES = ("免费AV在线看", "免費AV在線看")


class FreeJavBt:
    name = "freejavbt"

    def __init__(self, host: str = ""):
        if not host:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(host or HOST, interval=2.0)

    def crawler_original(self, code: str) -> CodeInfo | None:
        normalized = get_true_code(code)
        if not normalized:
            return None

        html = self.client.get(f"/zh/{normalized}")
        if not html:
            return None
        return html_to_code(html, normalized)


def html_to_code(html: str, code: str = "") -> CodeInfo | None:
    """解析 freejavbt 详情页。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] freejavbt 解析失败: {exc}")
        return None

    info = CodeInfo(code=get_true_code(code))

    for block in doc("div.single-video-meta").items():
        label = (block("span").eq(0).text() or "").strip().rstrip(":：")

        if label in ("女优", "女優"):
            # 演员链接里混着男优，取全部由上层去重；这里只做收集
            info.casts = join_list(a.text() for a in block("a").items())
            continue

        field = FIELD_MAP.get(label)
        if not field:
            continue

        links = [a.text() for a in block("a").items()]
        value = join_list(links) if links else (block("span").eq(1).text() or "").strip()
        if value and not getattr(info, field, ""):
            setattr(info, field, value)

    heading = (doc("h1").eq(0).text() or "").strip()
    if heading:
        prefix = info.code or code
        if prefix and heading.upper().startswith(prefix.upper()):
            heading = heading[len(prefix):].strip()
        for suffix in TITLE_SUFFIXES:
            if heading.endswith(suffix):
                heading = heading[: -len(suffix)].strip()
        info.title = heading

    # 番号不存在时站点仍返回 200，但字段区是空的
    if not info.release_date:
        logger.debug(f"[{code}] freejavbt 无此番号")
        return None

    return info if info.code and info.title else None
