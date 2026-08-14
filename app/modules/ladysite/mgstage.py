"""MGStage 解析。

素人系（SIRO、200GANA、259LUXU 等）的官方发行站，这类番号在 javbus/javdb
上信息往往缺失或错乱，官方站是唯一可靠来源。

两个坑：
1. 年龄确认门靠 `adc=1` Cookie 过，缺了拿回来的是确认页。与 dmm 同样由
   代码固定补上。
2. 详情页地址是 /product/product_detail/<番号>/，番号要保留原始大小写与
   横杠（SIRO-4321 不能拍平成 siro4321），与 dmm/jav321 的补零规则不同。
"""
from __future__ import annotations

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import (
    CodeInfo, SiteClient, absolute_url, join_list, parse_star,
)
from app.utils import get_true_code

HOST = "https://www.mgstage.com"

# 年龄确认门
AGE_COOKIE = "adc=1"

# 信息表是 <th>标签：</th><td>值</td>
FIELD_MAP = {
    "出演": "casts",
    "メーカー": "producer",
    "レーベル": "publisher",
    "シリーズ": "series",
    "配信開始日": "release_date",
    "商品発売日": "release_date",
    "収録時間": "duration",
    "ジャンル": "genres",
}


class Mgstage:
    name = "mgstage"

    def __init__(self, host: str = "", cookie: str = ""):
        if not host and not cookie:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                self._ensure_age_cookie()
                return

        self.client = SiteClient(host or HOST, cookie, interval=2.0, bypass_first=True)
        self._ensure_age_cookie()

    def _ensure_age_cookie(self) -> None:
        cookie = (self.client.cookie or "").strip()
        if "adc=" in cookie:
            return
        self.client.cookie = f"{cookie}; {AGE_COOKIE}".strip("; ") if cookie else AGE_COOKIE

    def crawler_original(self, code: str) -> CodeInfo | None:
        normalized = get_true_code(code)
        if not normalized:
            return None

        html = self.client.get(f"/product/product_detail/{normalized}/")
        if not html:
            return None
        return html_to_code(html, normalized, host=self.client.host)


def html_to_code(html: str, code: str = "", host: str = HOST) -> CodeInfo | None:
    """解析 MGStage 详情页。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] mgstage 解析失败: {exc}")
        return None

    info = CodeInfo(code=get_true_code(code))

    for row in doc("table tr").items():
        label = (row("th").eq(0).text() or "").strip().rstrip("：:")
        field = FIELD_MAP.get(label)
        if not field:
            continue

        cell = row("td").eq(0)
        if field in ("casts", "genres"):
            links = [a.text() for a in cell("a").items()]
            value = join_list(links) if links else (cell.text() or "").strip()
        else:
            value = (cell.text() or "").strip()
        # 「----」是站点表示"无"的占位
        if value.strip("-") == "":
            continue
        if value and not getattr(info, field, ""):
            setattr(info, field, value)

    if info.release_date:
        info.release_date = info.release_date.replace("/", "-")[:10]

    heading = (doc("h1.tag").eq(0).text() or doc("h1").eq(0).text() or "").strip()
    if heading:
        # 标题前挂着番号标签时剥掉
        prefix = info.code or code
        if prefix and heading.upper().startswith(prefix.upper()):
            heading = heading[len(prefix):].strip()
        info.title = heading

    star = doc(".user_review .review_average, .avg-rating").eq(0).text() or ""
    if star:
        info.star = parse_star(star)

    cover = doc("#EnlargeImage").attr("href") or doc(".detail_photo img").attr("src") or ""
    if not cover:
        cover = doc('meta[property="og:image"]').attr("content") or ""
    if cover:
        info.banner = absolute_url(cover, host)
        info.poster = info.banner

    stills = [
        absolute_url(node.attr("href") or node.attr("src") or "", host)
        for node in doc("#sample-photo a, .sample_image a").items()
    ]
    if stills:
        info.still_photo = join_list(stills)

    if not info.code:
        info.code = get_true_code(code)

    # 番号不存在时站点跳错误页，同样是 200；没有发售日就不是详情页
    if not info.release_date:
        logger.debug(f"[{code}] mgstage 无此作品或未过年龄确认")
        return None
    return info if info.code and info.title else None

