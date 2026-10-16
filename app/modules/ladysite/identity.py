"""在抓取边界验证页面身份；请求参数不能作为返回番号的证据。"""
from __future__ import annotations

import json
import re
from urllib.parse import unquote, urlparse

from pyquery import PyQuery

from app.utils import find_serial_number, find_serial_numbers, get_true_code


def same_code(actual: str, expected: str) -> bool:
    from app.modules.scrape.merge import base_code
    left, right = find_serial_number(actual) or get_true_code(actual), get_true_code(expected)
    return bool(left and right and (base_code(left) or left) == (base_code(right) or right))


def validate_page(html: str, final_url: str, expected: str) -> bool:
    """优先采用详情页编号/结构化编号，其次才是有编号的详情地址。"""
    if not html:
        return False
    url = unquote(final_url or '')
    cid = re.search(r'(?:cid=|[?&]id=)([a-z0-9_]+)', url, re.I)
    core = re.fullmatch(r'([A-Z]+)-(\d+)', expected)
    cid_matches = bool(cid and core and re.fullmatch(rf'(?:h_\d+|\d+)?{core[1]}0*{int(core[2])}', cid[1], re.I))
    if cid and core and not cid_matches:
        return False
    url_codes = find_serial_numbers(url)
    if not cid_matches and not expected.startswith('FC2-PPV-') and url_codes and not any(same_code(c, expected) for c in url_codes):
        return False
    try:
        payload = json.loads(html)
    except (ValueError, TypeError):
        payload = None
    doc = PyQuery(html) if payload is None else None
    if doc is not None:
        structured = doc('script#__NEXT_DATA__').text()
        if structured:
            try:
                payload = json.loads(structured).get('props', {}).get('pageProps', {}).get('work')
            except (ValueError, AttributeError):
                pass
        # 主详情编号的证据优先于标题，不能用推荐区的番号“证明”当前页。
        for node in doc('.info p, .movie-panel-info .panel-block, table tr').items():
            text = node.text()
            if re.search(r'識別碼|识别码|品番|商品番号|識別番号', text):
                codes = find_serial_numbers(text)
                if codes:
                    return any(same_code(c, expected) for c in codes)
    if isinstance(payload, dict):
        data = payload.get('data', payload)
        if isinstance(data, dict):
            for field in ('code', 'number', 'work_id', 'external_id', 'serial_number', 'barcode'):
                if data.get(field):
                    return same_code(str(data[field]), expected)
        return False
    if doc is not None:
        for selector in ('h2.title strong:first-child', 'h3', 'h1', 'title'):
            text = doc(selector).eq(0).text()
            codes = find_serial_numbers(text)
            if codes:
                return any(same_code(c, expected) for c in codes)

    # FC2 官方文章及 DMM/MGS/加勒比直达地址具有稳定的作品 ID。
    parsed = urlparse(url)
    if not parsed.scheme or re.search(r'/search|[?&](?:q|keyword|query)=', url, re.I):
        return False
    if expected.startswith('FC2-PPV-'):
        article = expected.removeprefix('FC2-PPV-')
        return bool(re.search(rf'/(?:article|video)/{re.escape(article)}(?:/|$|[?#])', url))
    codes = find_serial_numbers(url)
    if any(same_code(c, expected) for c in codes):
        return True
    # DMM 的 cid 带厂商前缀与补零；仅对明确 cid 参数比对，不接受任意 URL 子串。
    return cid_matches
