"""avbase 解析。

页面是 Next.js，数据都在 __NEXT_DATA__ 的 JSON 里，
比解析 HTML 稳定得多——改版动的是模板，JSON 结构一般不变。

一个作品可能有多个发行版本（products），取第一个有图的。
直连稳定 403，必须配 BYPASS_URL。
"""
from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import quote

from loguru import logger

from app.modules.ladysite.base import CodeInfo, SiteClient, join_list
from app.utils import get_true_code

HOST = "https://www.avbase.net"

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
    re.S,
)

# JS 的日期字符串形如 "Thu Feb 18 2021 09:00:00 GMT+0900 (Japan Standard Time)"
MONTHS = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
    "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}
JS_DATE_RE = re.compile(r"^\w{3} (\w{3}) (\d{1,2}) (\d{4})")


class Avbase:
    name = "avbase"

    def __init__(self, host: str = ""):
        if not host:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(host or HOST, interval=2.0, bypass_first=True)

    def crawler_original(self, code: str) -> CodeInfo | None:
        normalized = get_true_code(code)
        if not normalized:
            return None

        html = self.client.get(f"/works/{normalized}")
        if not html:
            return None
        return html_to_code(html, normalized)

    def feed(self, day='', page=1):
        path='/works/date/'+date.fromisoformat(day).isoformat() if day else '/works/recent'
        return html_to_works(self.client.get(path,params={'page':page},timeout=40))

    def actor_works(self, name, page=1, single=False):
        from app.utils.actornames import matches
        path='/talents/'+quote(name.strip(),safe='')
        params={'page':page}
        if single:params['cast_size']='single'
        html=self.client.get(path,params=params,timeout=40)
        props=page_props(html);talent=props.get('talent') or {};primary=talent.get('primary') or {}
        canonical=primary.get('name') or name
        aliases=list(dict.fromkeys(a.get('name') for a in talent.get('actors') or [] if a.get('name')))
        if not talent or not matches(name,canonical,aliases):raise ValueError('Avbase 未返回匹配的演员资料')
        movies,_=html_to_works(html)
        # The source's single-cast filter is also checked locally.
        if single:movies=[row for row in movies if len([n for n in (row.get('casts') or '').split(',') if n])==1]
        maximum=props.get('max_page')
        has_more=page<int(maximum) if maximum else len(props.get('works') or [])>=30
        return dict(name=canonical,aliases=aliases,photo=primary.get('image_url') or '',source_url=self.client.host+path,items=movies,has_more=has_more)


def page_props(html):
    match=NEXT_DATA_RE.search(html or '')
    if not match:raise ValueError('Avbase 页面没有作品数据，可能仍在验证页')
    props=(json.loads(match[1]).get('props') or {}).get('pageProps') or {}
    if not isinstance(props.get('works'),list):raise ValueError('Avbase 列表数据格式不匹配')
    return props


def html_to_works(html):
    props=page_props(html);movies=[];actors=[];seen=set()
    for work in props['works'][:100]:
        work=dict(work)
        work['title']=work.get('title') or next((p.get('title') for p in work.get('products') or [] if p.get('title')),'')
        if not work.get('casts'):work['casts']=[{'actor':actor} for actor in work.get('actors') or []]
        payload=json.dumps({'props':{'pageProps':{'work':work}}},ensure_ascii=False).replace('</','<\\/')
        info=html_to_code('<script id="__NEXT_DATA__" type="application/json">'+payload+'</script>')
        if info and info.code not in seen:movies.append(info.to_dict());seen.add(info.code)
        actors.extend({'name':a.get('name'),'photo':a.get('image_url')} for a in work.get('actors') or [] if a.get('name'))
    if props['works'] and not movies:raise ValueError('Avbase 列表没有可识别的影片')
    return movies,actors


def _js_date(raw: str) -> str:
    """JS 日期字符串转 YYYY-MM-DD。认不出就返回空串。"""
    match = JS_DATE_RE.match((raw or "").strip())
    if not match:
        return ""
    month, day, year = match.groups()
    if month not in MONTHS:
        return ""
    return f"{year}-{MONTHS[month]}-{int(day):02d}"


def html_to_code(html: str, code: str = "") -> CodeInfo | None:
    """从 __NEXT_DATA__ 里取作品信息。"""
    match = NEXT_DATA_RE.search(html or "")
    if not match:
        logger.debug(f"[{code}] avbase 页面没有 __NEXT_DATA__")
        return None

    try:
        payload = json.loads(match.group(1))
        work = (payload.get("props", {}).get("pageProps", {}) or {}).get("work") or {}
    except (ValueError, AttributeError) as exc:
        logger.debug(f"[{code}] avbase JSON 解析失败: {exc}")
        return None

    if not work:
        return None

    info = CodeInfo(code=get_true_code(work.get("work_id") or code))
    info.title = (work.get("title") or "").strip()
    info.outline = str(work.get('description') or work.get('outline') or '').strip()
    info.release_date = _js_date(work.get("min_date") or "")

    info.casts = join_list(
        (item.get("actor") or {}).get("name") for item in work.get("casts") or []
    )
    info.genres = join_list(
        item.get("name") for item in work.get("genres") or []
    )

    # 同一作品可能有多个发行版本，取第一个带图的
    products = work.get("products") or []
    product = next((p for p in products if p.get("image_url")), None) or (
        products[0] if products else {}
    )
    if product:
        info.outline = info.outline or str(product.get('description') or '').strip()
        info.producer = ((product.get("maker") or {}).get("name") or "").strip()
        info.publisher = ((product.get("label") or {}).get("name") or "").strip()
        info.series = ((product.get("series") or {}).get("name") or "").strip()

        cover = product.get("image_url") or product.get("thumbnail_url") or ""
        if cover:
            info.banner = cover
            info.poster = cover

        # 剧照是 {"s": 小图, "l": 大图} 的数组，取大图
        info.still_photo = join_list(
            (item or {}).get("l") or (item or {}).get("s")
            for item in product.get("sample_image_urls") or []
        )

        if not info.release_date:
            info.release_date = _js_date(product.get("date") or "")

    return info if info.code and info.title else None
