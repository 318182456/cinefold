"""Airav 解析。

带中文标题与中文类别，是 missav 之外的另一个中文来源 —— missav 必须过盾，
Airav 有 JSON 接口，可靠性更好。

站点自带 API：/api/video/barcode/<番号>?lng=zh-TW 直接返回 JSON，
不必解 HTML。改版动 HTML 模板的概率远高于动接口字段，走接口更稳。
接口不通时（站点关了 API 或换了路径）回落到解详情页 HTML。
"""
from __future__ import annotations

import json
import re

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import CodeInfo, SiteClient, join_list
from app.utils import get_true_code

# 简介的字数上限。Emby 的简介栏折叠后只露前几行，而 AI 看点还要拼在
# 简介前面（见 services/review.py 的 merge_text）—— 官方简介太长会把
# 看点挤到折叠线以下，等于白写
OUTLINE_MAX_CHARS = 500

HOST = "https://airav.io"

# 详情页信息区形如 <li><span>發行日期</span>2021-02-19</li>
FIELD_MAP = {
    "發行日期": "release_date",
    "发行日期": "release_date",
    "播放時間": "duration",
    "播放时间": "duration",
    "片商": "producer",
    "系列": "series",
    "女優": "casts",
    "女优": "casts",
    "類型": "genres",
    "类型": "genres",
}


class Airav:
    name = "airav"

    def __init__(self, host: str = "", cookie: str = ""):
        if not host and not cookie:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(host or HOST, cookie, interval=2.0, bypass_first=True)

    def crawler_original(self, code: str) -> CodeInfo | None:
        normalized = get_true_code(code)
        if not normalized:
            return None

        # 先走 JSON 接口。host 可能带 /cn 之类的语言前缀，接口在站点根上，
        # 所以从 host 里剥掉路径部分
        root = _root(self.client.host)
        raw = self.client.get(
            f"{root}/api/video/barcode/{normalized}", params={"lng": "zh-TW"}
        )
        info = json_to_code(raw, normalized) if raw else None
        if info:
            return info

        html = self.client.get(f"/video/{normalized}")
        if not html:
            return None
        return html_to_code(html, normalized)


def _root(host: str) -> str:
    """去掉 host 上的语言路径，只留 scheme://domain。

    内置默认地址是 https://airav.io/cn，直接拼接口路径会变成 /cn/api/...
    """
    parts = (host or "").split("/")
    return "/".join(parts[:3]) if len(parts) >= 3 else host


def _clean_outline(raw: object) -> str:
    """清洗简介文本。

    三件事：
      去 HTML  接口偶尔把 <br> 之类塞在简介里，写进 NFO 会原样显示出来
      繁转简   接口按 lng=zh-TW 请求（见文件头），拿到的是繁体。
               媒体库里其余字段都是简体，混排很难看
      掐长度   个别条目的 description 是整段营销文案，几千字灌进 Emby
               的简介栏会把页面撑爆，而 AI 看点还要拼在前面

    截断落在句末而不是硬切：切在半句上比短一点更难读。
    """
    text = (raw or "")
    if not isinstance(text, str) or not text.strip():
        return ""

    from app.modules.subtitle.t2s import to_simplified

    # <br> 之类先换成空格，再统一清标签，避免相邻两句黏在一起
    text = re.sub(r"<\s*br\s*/?\s*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""

    text = to_simplified(text)

    if len(text) > OUTLINE_MAX_CHARS:
        head = text[:OUTLINE_MAX_CHARS]
        # 往回找最近的句末标点，找不到就认了硬切
        cut = max(head.rfind(mark) for mark in "。！？.!?")
        text = head[: cut + 1] if cut > OUTLINE_MAX_CHARS // 2 else head
    return text


def json_to_code(raw: str, code: str = "") -> CodeInfo | None:
    """解析 Airav 的 JSON 接口响应。"""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        # 接口没了会返回 HTML，交给 HTML 分支处理，不算错误
        return None

    if not isinstance(payload, dict):
        return None
    # 番号不存在时返回 {"count": 0} 或空 barcode
    if not payload.get("barcode") and not payload.get("name"):
        return None

    info = CodeInfo(code=get_true_code(payload.get("barcode") or code))
    info.title = (payload.get("name") or "").strip()
    info.outline = _clean_outline(payload.get("description"))
    info.release_date = (payload.get("publish_date") or "")[:10]

    info.casts = join_list(
        (item or {}).get("name") if isinstance(item, dict) else item
        for item in payload.get("actors") or []
    )
    info.genres = join_list(
        (item or {}).get("name") if isinstance(item, dict) else item
        for item in payload.get("tags") or []
    )

    factory = payload.get("factories") or []
    if factory:
        first = factory[0]
        info.producer = ((first or {}).get("name") if isinstance(first, dict) else first) or ""

    cover = payload.get("img_url") or payload.get("images") or ""
    if isinstance(cover, list):
        cover = cover[0] if cover else ""
    if cover:
        info.banner = cover
        info.poster = cover

    info.still_photo = join_list(
        item if isinstance(item, str) else (item or {}).get("url")
        for item in payload.get("images") or []
        if not isinstance(payload.get("images"), str)
    )

    if not info.code:
        info.code = get_true_code(code)
    return info if info.code and info.title else None


def html_to_code(html: str, code: str = "") -> CodeInfo | None:
    """解析 Airav 详情页 HTML。接口不可用时的回落路径。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] airav 解析失败: {exc}")
        return None

    info = CodeInfo(code=get_true_code(code))

    for row in doc(".video-info li, .about-info li, .detail-info li").items():
        label = (row("span").eq(0).text() or "").strip().rstrip("：: ")
        field = FIELD_MAP.get(label)
        if not field:
            continue

        if field in ("casts", "genres"):
            value = join_list(a.text() for a in row("a").items())
        else:
            # 标签在 span 里，值是 li 的其余文本
            value = (row.text() or "").replace(label, "", 1).strip("：: ")
        if value and not getattr(info, field, ""):
            setattr(info, field, value)

    heading = (doc("h1").eq(0).text() or doc(".video-title").eq(0).text() or "").strip()
    if heading:
        prefix = info.code or code
        if prefix and heading.upper().startswith(prefix.upper()):
            heading = heading[len(prefix):].strip(" -")
        info.title = heading

    cover = doc('meta[property="og:image"]').attr("content") or ""
    if cover:
        info.banner = cover
        info.poster = cover

    if not info.code:
        info.code = get_true_code(code)

    # 番号不存在时站点返回 200 的搜索页，没有发行日期
    if not info.release_date:
        logger.debug(f"[{code}] airav 无此番号")
        return None
    return info if info.code and info.title else None
