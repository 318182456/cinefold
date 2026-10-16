"""AV-Wiki 素人资料补充，参考 AVM 的 dl 字段与真名识别思路。"""
from __future__ import annotations

import re

from pyquery import PyQuery

from app.modules.ladysite.base import CodeInfo, SiteClient, join_list, normalize_date
from app.modules.ladysite.identity import validate_page
from app.utils import get_true_code


class AvWiki:
    name = 'avwiki'

    def __init__(self):
        self.client = SiteClient.from_source(self.name) or SiteClient('https://av-wiki.net', interval=2, bypass_first=True)

    def crawler_original(self, code: str) -> CodeInfo | None:
        code = get_true_code(code)
        page = self.client.get(f'/{code.lower()}/')
        if not page or not validate_page(page, self.client.last_url, code):
            return None
        doc = PyQuery(page)
        for node in doc('script, style, nav, footer, form, .comments').items():
            node.remove()
        info = CodeInfo(code=code)
        info.identity_verified = True
        info.title = doc('h1').eq(0).text().strip()
        fields = {n.text().strip().rstrip(':：'): n.next('dd').text().strip() for n in doc('dl dt').items()}
        body = doc('.entry-content, article').eq(0).text() or doc('body').text()
        names = fields.get('AV女優名') or ''
        if not names:
            match = re.search(r'AV女優名[：:]\s*([^\s、,]+)|名前は、\s*([^\s、,]+?)さん', body)
            names = next((v for v in match.groups() if v), '') if match else ''
        info.casts = join_list(re.split(r'[、,／/]', names))
        info.producer = fields.get('配信メーカー') or fields.get('メーカー') or ''
        info.series = fields.get('シリーズ') or ''
        info.release_date = normalize_date(fields.get('配信開始日') or fields.get('発売日') or '')
        # 不将化名或卖家伪装成另一位演员；无真名时交给其他源补全。
        return info if info.title else None
