"""DMM（FANZA）解析。

官方发行站，片商/レーベル/シリーズ/发售日是一手数据，比二手站准确。
封面与剧照走 pics.dmm.co.jp 图床，尺寸最大。

两个必须处理的坑：
1. 全站有年龄确认门。不带 `age_check_done=1` Cookie 会被 302 到
   /age_check/，抓回来的是确认页而非详情页 —— 所以这个 Cookie 由代码
   固定补上，不依赖用户去数据源页配。
2. 详情页 URL 里的 cid 不是番号：SSIS-001 → ssis00001（数字补到 5 位），
   与 jav321 同一套规则。但 DMM 同一作品存在 digital / mono 等多条线路，
   cid 前缀也可能带 h_ 之类，因此推算失败时回落到站内搜索。
"""
from __future__ import annotations

import re

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import CodeInfo, SiteClient, join_list, parse_star
from app.utils import get_true_code

HOST = "https://www.dmm.co.jp"

# 年龄确认门。不带这个 Cookie 拿回来的永远是确认页
AGE_COOKIE = "age_check_done=1"

# 详情页信息表是 <td>标签</td><td>值</td>
FIELD_MAP = {
    "商品発売日": "release_date",
    "配信開始日": "release_date",
    "収録時間": "duration",
    "メーカー": "producer",
    "レーベル": "publisher",
    "シリーズ": "series",
    "出演者": "casts",
    "出演": "casts",
    "ジャンル": "genres",
}

# 「品番：ssis00001」——用于校验抓到的是不是目标作品
CID_RE = re.compile(r"cid=([a-z0-9_]+)", re.I)

# 评分形如「★4.52」或「評価： 4.52点」
STAR_RE = re.compile(r"([0-9]\.[0-9]+)")


class Dmm:
    name = "dmm"

    def __init__(self, host: str = "", cookie: str = ""):
        if not host and not cookie:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                self._ensure_age_cookie()
                return

        self.client = SiteClient(host or HOST, cookie, interval=2.0)
        self._ensure_age_cookie()

    def _ensure_age_cookie(self) -> None:
        """把年龄确认 Cookie 并进用户配置里，缺了整站抓不到东西。"""
        cookie = (self.client.cookie or "").strip()
        if "age_check_done" in cookie:
            return
        self.client.cookie = f"{cookie}; {AGE_COOKIE}".strip("; ") if cookie else AGE_COOKIE

    def crawler_original(self, code: str) -> CodeInfo | None:
        normalized = get_true_code(code)
        if not normalized:
            return None

        html = ""
        cid = _to_cid(normalized)
        if cid:
            # digital/videoa 是有码作品的主线路，命中率最高
            html = self.client.get(f"/digital/videoa/-/detail/=/cid={cid}/")

        # cid 推算不中（换了线路或带 h_ 前缀）时回落到搜索
        if not _looks_like_detail(html):
            path = self.search_detail_url(normalized)
            html = self.client.get(path) if path else ""

        if not _looks_like_detail(html):
            return None
        return html_to_code(html, normalized)

    def search_detail_url(self, code: str) -> str:
        html = self.client.get(
            "/search/", params={"searchstr": code.replace("-", "")}
        )
        return html_to_detail_url(html, code) if html else ""


def _to_cid(code: str) -> str:
    """SSIS-001 → ssis00001。数字段补零到 5 位，与 jav321 同规则。"""
    if "-" not in (code or ""):
        return ""
    prefix, _, number = code.rpartition("-")
    if not prefix or not number.isdigit():
        return ""
    return f"{prefix.lower().replace('-', '')}{int(number):05d}"


def _looks_like_detail(html: str) -> bool:
    """区分详情页与年龄确认页/搜索无结果页。

    年龄门与错误页都是 200，只看状态码分不出来；详情页必定有信息表。
    """
    if not html:
        return False
    if "age_check" in html and "mu-pageInfo" not in html:
        return False
    return "informationList" in html or "品番" in html or "商品発売日" in html


def html_to_detail_url(html: str, code: str = "") -> str:
    """搜索结果里找番号匹配的详情链接。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] dmm 解析搜索页失败: {exc}")
        return ""

    target = _to_cid(get_true_code(code))
    fallback = ""
    for link in doc("a").items():
        href = link.attr("href") or ""
        if "/detail/" not in href:
            continue
        match = CID_RE.search(href)
        if not match:
            continue
        cid = match.group(1).lower()
        # 精确命中优先；带 h_ 之类前缀的同作品作为兜底
        if cid == target:
            return href
        if not fallback and target and target in cid:
            fallback = href
    return fallback


def html_to_code(html: str, code: str = "") -> CodeInfo | None:
    """解析 DMM 详情页。"""
    try:
        doc = PyQuery(html)
    except Exception as exc:
        logger.debug(f"[{code}] dmm 解析失败: {exc}")
        return None

    info = CodeInfo(code=get_true_code(code))

    # 信息表：<tr><td>ラベル：</td><td>値</td></tr>
    for row in doc("table tr").items():
        cells = row("td")
        if len(cells) < 2:
            continue
        label = (PyQuery(cells[0]).text() or "").strip().rstrip("：:")
        field = FIELD_MAP.get(label)
        if not field:
            continue

        cell = PyQuery(cells[1])
        if field in ("casts", "genres"):
            value = join_list(a.text() for a in cell("a").items())
        else:
            value = (cell.text() or "").strip()
        # 「----」是 DMM 表示"无"的占位
        if value.strip("-") == "":
            continue
        if value and not getattr(info, field, ""):
            setattr(info, field, value)

    # 发售日形如 2021/02/19，统一成 2021-02-19
    if info.release_date:
        info.release_date = info.release_date.replace("/", "-")[:10]

    heading = (doc("h1").eq(0).text() or "").strip()
    if heading:
        info.title = heading

    star = doc(".dcd-review__average, .d-review__average").eq(0).text() or ""
    if star:
        info.star = parse_star(STAR_RE.search(star).group(1)) if STAR_RE.search(star) else None

    # 封面：pl 是大图，ps 是缩略图
    cover = doc('meta[property="og:image"]').attr("content") or ""
    if not cover:
        for img in doc("img").items():
            src = img.attr("src") or ""
            if "pl.jpg" in src:
                cover = src
                break
    if cover:
        cover = cover.replace("ps.jpg", "pl.jpg")
        info.banner = cover
        info.poster = cover

    # 剧照缩略图形如 ...-1.jpg，大图是 ...jp-1.jpg
    stills = []
    for img in doc("#sample-image-block img, .sample-image-block img").items():
        src = img.attr("src") or ""
        if src:
            stills.append(re.sub(r"-(\d+)\.jpg$", r"jp-\1.jpg", src))
    if stills:
        info.still_photo = join_list(stills)

    if not info.code:
        info.code = get_true_code(code)

    # 年龄门与错误页都能被 PyQuery 解析出 h1，靠发售日拦掉非详情页
    if not info.release_date:
        logger.debug(f"[{code}] dmm 无此作品或未过年龄确认")
        return None
    return info if info.code and info.title else None
