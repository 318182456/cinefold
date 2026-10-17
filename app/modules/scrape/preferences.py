"""刮削专用的类型、字段优先级。数据源开关与番号规则仍由数据源页面管理。"""
from __future__ import annotations

import re

from app.modules.ladysite.sources import _AMATEUR, _DOMESTIC, _UNCENSORED, code_allowed

CATEGORIES = {"censored", "uncensored", "amateur", "fc2", "domestic"}
FIELDS = {"title", "cn_title", "outline", "casts", "banner", "poster", "still_photo", "genres", "star", "release_date", "duration", "publisher", "producer", "series"}
ALIASES = {"airav_io": "airav", "hbox_jp": "hbox", "mmtv": "7mmtv", "miss_av": "missav", "fc2_hub": "fc2hub"}


def sites(value) -> list[str]:
    values = value if isinstance(value, list) else re.split(r"[,，\s]+", value) if isinstance(value, str) else []
    return list(dict.fromkeys(ALIASES.get(str(v).lower().strip(), str(v).lower().strip()) for v in values if str(v).strip()))


def category(code: str) -> str:
    if code.upper().startswith("FC2"):
        return "fc2"
    if code_allowed(code, f"only:{_DOMESTIC}"):
        return "domestic"
    if code_allowed(code, f"only:date,{_UNCENSORED}"):
        return "uncensored"
    if code_allowed(code, f"only:{_AMATEUR}"):
        return "amateur"
    return "censored"


def ordered(code: str, available, preferences: dict) -> list[str]:
    preferred = sites(preferences.get(category(code), "")) if isinstance(preferences, dict) else []
    return [s for s in preferred if s in available] + [s for s in available if s not in preferred]


def validate(config: dict) -> str:
    if 'scrape_duplicate_policy' in config and config['scrape_duplicate_policy'] not in ('versions', 'overwrite', 'highest_quality', 'keep_existing'):
        return '重复影片处理方式必须为覆盖、保留高清、保留旧的或保留两个版本'
    from app.modules.ladysite import DETAIL_SITES, SPECIAL_SITES
    supported = set(DETAIL_SITES + SPECIAL_SITES)
    from app.modules.ladysite.sources import enabled_parser_sources
    try:
        supported.update(s['key'] for s in enabled_parser_sources())
    except Exception:
        pass
    if 'scrape_poster_crop' in config and config['scrape_poster_crop'] not in ('auto', 'always', 'never'):
        return '海报裁切模式必须为 auto、always 或 never'
    if "scrape_watermark" in config:
        from app.modules.scrape.watermark import validate as validate_watermark
        error = validate_watermark(config["scrape_watermark"])
        if error:
            return error
    for name, keys in (("scrape_source_priority", CATEGORIES), ("scrape_field_priority", FIELDS), ("scrape_field_exclude", FIELDS)):
        if name not in config:
            continue
        value = config[name]
        if not isinstance(value, dict) or set(value) - keys:
            return f"{name}: 配置格式或字段名称无效"
        for key, raw in value.items():
            if not isinstance(raw, str) and not (isinstance(raw, list) and all(isinstance(v, str) for v in raw)):
                return f"{name}.{key}: 请使用有序的来源列表"
            invalid = set(sites(raw)) - supported
            if invalid:
                return f"{name}.{key}: 不支持的解析源 {', '.join(sorted(invalid))}"
    return ""
