"""配置驱动的 JSON / CSS 数据源，使用现有抓取、身份校验和合并流程。"""
from __future__ import annotations

import json
from urllib.parse import quote, urljoin

from pyquery import PyQuery

from app.modules.ladysite.base import CodeInfo, SiteClient
from app.modules.ladysite.identity import same_code
from app.utils import get_true_code

FIELDS = set(CodeInfo.__dataclass_fields__) - {'code'}


def validate_options(parser: str, options: dict) -> str:
    if parser not in ('', 'http_json', 'http_html'):
        return '解析方式必须为 http_json 或 http_html'
    if not parser:
        return ''
    if not isinstance(options, dict) or set(options) - {'detail_url', 'fields', 'selectors'}:
        return '规则只支持 detail_url、fields、selectors'
    if not isinstance(options.get('detail_url'), str) or '{code}' not in options['detail_url']:
        return '详情地址模板需要包含 {code}'
    rules = options.get('fields' if parser == 'http_json' else 'selectors')
    if not isinstance(rules, dict) or not rules.get('code') or not rules.get('title'):
        return '规则必须配置真实 code 和 title 字段'
    if set(rules) - (FIELDS | {'code'}):
        return '规则包含不支持的元数据字段'
    if not all(isinstance(v, str) and v for v in rules.values()):
        return '字段规则必须是非空字符串'
    template = options['detail_url']
    if template.replace('{code}', '').find('{') >= 0 or template.replace('{code}', '').find('}') >= 0:
        return '地址模板仅支持 {code} 变量'
    return ''


def dig(value, path: str):
    for key in path.split('.'):
        if isinstance(value, list) and key.isdigit():
            value = value[int(key)] if int(key) < len(value) else None
        elif isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return value


class CustomSource:
    def __init__(self, source: dict):
        self.name = source['key']
        self.parser, self.options = source['parser'], source.get('options') or {}
        self.client = SiteClient.from_source(self.name)

    def crawler_original(self, code: str) -> CodeInfo | None:
        if validate_options(self.parser, self.options) or self.client is None:
            return None
        code = get_true_code(code)
        url = self.options['detail_url'].replace('{code}', quote(code, safe=''))
        raw = self.client.get(url)
        if not raw:
            return None
        values = {}
        if self.parser == 'http_json':
            try:
                payload = json.loads(raw)
            except ValueError:
                return None
            for field, path in self.options['fields'].items():
                values[field] = dig(payload, path)
        else:
            doc = PyQuery(raw)
            for field, rule in self.options['selectors'].items():
                selector, _, attr = rule.partition('::attr(')
                nodes = doc(selector)
                parts = [(node.attr(attr.rstrip(')')) if attr else node.text()) or '' for node in nodes.items()]
                values[field] = ','.join(x.strip() for x in parts if x.strip())
        if not same_code(str(values.pop('code', '')), code):
            return None
        info = CodeInfo(code=code)
        info.identity_verified = True
        for field, value in values.items():
            if field not in FIELDS or value in (None, '', []):
                continue
            if isinstance(value, list):
                value = ','.join(str(x.get('name', '')) if isinstance(x, dict) else str(x) for x in value)
            if field in ('banner', 'poster', 'preview_url'):
                value = urljoin(self.client.last_url, str(value))
            elif field == 'still_photo':
                value = ','.join(urljoin(self.client.last_url, x.strip()) for x in str(value).split(',') if x.strip())
            setattr(info, field, value)
        return info if info.title else None
