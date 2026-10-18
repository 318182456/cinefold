"""演员字段清洗；保留独立姓名与明确括号别名，不按空格拆英文名。"""
import html
import json
import re
import unicodedata

_EMPTY_NAMES = {'unknown', 'none', 'null', 'n/a', 'n.a.', 'nan', '不明', '未知', '未提供', '無名', '匿名', '素人', '演员', '演員', '出演', '男優', '女優', '女优', '男优'}


def normalize(value):
    return unicodedata.normalize('NFKC', html.unescape(str(value or ''))).strip()


def valid(value):
    value = normalize(value)
    if not value or len(value) > 128 or not any(char.isalpha() for char in value):
        return False
    if re.search(r'[<>{}\[\]="\\]|https?://|www\.', value, re.I):
        return False
    if re.fullmatch(r'\d+\s*[歳岁才]', value):
        return False
    return value.casefold() not in _EMPTY_NAMES


def names(value):
    if isinstance(value, (list, tuple)):
        return list(dict.fromkeys(name for part in value for name in names(part)))
    if isinstance(value, dict):
        return names(value.get('name', ''))
    raw = normalize(value)
    if raw.casefold() in _EMPTY_NAMES:
        return []
    if raw.startswith(('[', '{')):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, (list, dict)):
                return names(parsed)
        except (ValueError, TypeError):
            return []
    out = []
    for part in re.split(r'[,，、;；|/\n]+', raw):
        match = re.fullmatch(r'([^()]+)\(([^()]+)\)', part.strip())
        for name in ([match[1], match[2]] if match else [part]):
            name = name.strip()
            if valid(name) and name not in out:
                out.append(name)
    return out


def matches(requested, *values):
    key = re.sub(r'\s+', '', normalize(requested)).casefold()
    return valid(requested) and any(key == re.sub(r'\s+', '', name).casefold() for value in values for name in names(value))


def native(value):
    return bool(re.search(r'[\u3040-\u30ff\u3400-\u9fff]', normalize(value)))


def presentation(name, aliases=(), preferred=''):
    """仅使用已关联的姓名，不将罗马音猜译成汉字。name 保留为请求/订阅键。"""
    known = names([name, *names(aliases)])
    label = name if native(name) else next((value for value in [preferred, *known] if value in known and native(value)), name)
    return {'display_name': label, 'aliases': [value for value in known if value != label],
            'needs_name_lookup': not native(label)}


def cast_names(value):
    """影片演员字段中显式括号别名只输出一个人名。"""
    if isinstance(value, (list, tuple)):
        return list(dict.fromkeys(name for part in value for name in cast_names(part)))
    if isinstance(value, dict):
        return cast_names(value.get('name', ''))
    if isinstance(value, bool):
        return []
    raw = normalize(value)
    if raw.casefold() in _EMPTY_NAMES:
        return []
    if raw.startswith(('[', '{')):
        try:
            return cast_names(json.loads(raw))
        except (ValueError, TypeError):
            return []
    out = []
    for part in re.split(r'[,，、;；|/\n]+', raw):
        match = re.fullmatch(r'([^()]+)\(([^()]+)\)', part.strip())
        values = names(part)
        if match and values:
            values = [presentation(values[0], values[1:])['display_name']]
        for name in values:
            if name not in out:
                out.append(name)
    return out
