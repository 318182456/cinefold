"""Bound AI request sizes without truncating the library."""
import json
import re
import unicodedata


def batches(items):
    batch, size = [], 0
    for item in items:
        safe = {key: str(item.get(key) or '')[:limit] for key, limit in (
            ('code',64), ('title',180), ('cn_title',180), ('series',100), ('casts',160),
            ('genres',180), ('outline',220), ('release_date',32))}
        length = len(json.dumps(safe, ensure_ascii=False))
        if batch and (len(batch) >= 80 or size + length > 80000):
            yield batch
            batch, size = [], 0
        batch.append(safe)
        size += length
    if batch:
        yield batch


def explicit_groups(items, mode):
    """Whole-library metadata groups bridge independently analyzed batches."""
    grouped = {}
    for item in items:
        for field, kind in (('casts', 'actor'), ('series', 'series')):
            if mode not in ('all', kind):
                continue
            values = re.split(r'[,，、/|;；\n]+', str(item.get(field) or ''))
            for value in values:
                value = unicodedata.normalize('NFKC', value).strip()
                if not value or value.casefold() in ('未知', 'unknown', 'n/a', 'none', '素人'):
                    continue
                row = grouped.setdefault((kind, value.casefold()),
                    {'name': f'{"演员" if kind=="actor" else "系列"} · {value}'[:128], 'kind':kind,
                     'reason':'根据全库已有演员或系列字段归类', 'confidence':1, 'codes':[]})
                if item['code'] not in row['codes']:
                    row['codes'].append(item['code'])
    return [row for row in grouped.values() if len(row['codes']) >= 2]


def merge_candidates(candidates):
    grouped = {}
    for row in candidates:
        name = unicodedata.normalize('NFKC', str(row.get('name') or '')).strip()
        key = row.get('kind'), name.casefold()
        if key not in grouped:
            grouped[key] = {**row, 'name':name, 'codes':[]}
        grouped[key]['codes'] = list(dict.fromkeys(grouped[key]['codes'] + list(row.get('codes') or [])))
    # Identical membership with different AI names is one candidate per kind.
    seen, result = set(), []
    for row in grouped.values():
        key = row.get('kind'), tuple(sorted(row['codes']))
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result
