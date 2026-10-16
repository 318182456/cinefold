"""共享的元数据条件匹配、订阅规则和每番号下载方案。"""
from __future__ import annotations

import json
import re
import threading
from datetime import date

from sqlalchemy import exists, select

from app.database.models import Code, CodeProfile, CodeStatus, DownloadProfile, RuleExclusion, SubscriptionRule
from app.database.session import session_scope
from app.utils.filters import has_vr

_run_lock = threading.Lock()
CONDITION_KEYS = {"prefixes", "publisher", "producer", "series", "genres", "casts", "genre_mode", "earliest_release_date", "max_coactors", "exclude_vr"}


def terms(value) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
        if isinstance(parsed, list):
            return terms(parsed)
    except (ValueError, TypeError):
        pass
    return [x.strip() for x in re.split(r"[,，|\n]+", str(value)) if x.strip()]


def validate_conditions(conditions: dict, *, allow_empty: bool = False) -> dict:
    unknown = set(conditions) - CONDITION_KEYS
    if unknown:
        raise ValueError(f"未知条件：{', '.join(sorted(unknown))}")
    result = dict(conditions)
    for key in ("prefixes", "genres", "casts"):
        if key in result:
            result[key] = terms(result[key])
    for key in ("publisher", "producer", "series"):
        if key in result:
            if not isinstance(result[key], str):
                raise ValueError(f"{key} 必须为文本")
            result[key] = result[key].strip()
    if result.get("genre_mode", "all") not in ("all", "any"):
        raise ValueError("标签匹配方式必须是 all 或 any")
    if result.get("earliest_release_date"):
        date.fromisoformat(result["earliest_release_date"])
    if "exclude_vr" in result and not isinstance(result["exclude_vr"], bool):
        raise ValueError("exclude_vr 必须为布尔值")
    if result.get("max_coactors") not in (None, ""):
        result["max_coactors"] = int(result["max_coactors"])
        if result["max_coactors"] < 1:
            raise ValueError("出演人数上限必须大于 0")
    if not allow_empty and not any(result.get(k) for k in CONDITION_KEYS - {"genre_mode", "exclude_vr"}):
        raise ValueError("至少设置一个订阅条件，避免订阅整个资料库")
    return result


def matches(item: dict, conditions: dict) -> bool:
    code = (item.get("code") or "").upper()
    prefixes = terms(conditions.get("prefixes"))
    if prefixes and not any(code.split("-")[0] == p.upper().rstrip("-") for p in prefixes):
        return False
    for key in ("publisher", "producer", "series"):
        if conditions.get(key) and conditions[key].casefold() != (item.get(key) or "").casefold():
            return False
    for key in ("genres", "casts"):
        wanted = {x.casefold() for x in terms(conditions.get(key))}
        actual = {x.casefold() for x in terms(item.get(key))}
        if wanted:
            if key == "genres" and conditions.get("genre_mode") == "any":
                if not wanted & actual:
                    return False
            elif not wanted <= actual:
                return False
    earliest = conditions.get("earliest_release_date")
    if earliest and (not item.get("release_date") or item["release_date"][:10] < earliest):
        return False
    maximum = conditions.get("max_coactors")
    if maximum and (not terms(item.get("casts")) or len(terms(item["casts"])) > int(maximum)):
        return False
    if conditions.get("exclude_vr") and has_vr(f"{code} {item.get('title') or ''} {item.get('genres') or ''}"):
        return False
    return True


def effective_profile(code: str) -> DownloadProfile | None:
    with session_scope() as session:
        assignment = session.get(CodeProfile, code)
        return session.get(DownloadProfile, assignment.profile_id) if assignment else None


def preview_rule(conditions: dict, limit: int = 50) -> dict:
    conditions = validate_conditions(conditions)
    count, items = 0, []
    with session_scope() as session:
        for row in session.scalars(select(Code).order_by(Code.release_date.desc())).yield_per(500):
            if matches(row.to_dict(), conditions):
                count += 1
                if len(items) < limit:
                    items.append(row.to_dict())
    return {"total": count, "items": items}


def run_rules() -> int:
    """只订阅尚未订阅的资料；不重置下载中、已入库或已失败条目。"""
    if not _run_lock.acquire(blocking=False):
        return 0
    try:
        return _run_rules()
    finally:
        _run_lock.release()


def _run_rules() -> int:
    from app.services import is_filtered_code

    count = 0
    with session_scope() as session:
        rules = session.scalars(select(SubscriptionRule).where(SubscriptionRule.enabled.is_(True)).order_by(SubscriptionRule.id)).all()
        if not rules:
            return 0
        query = select(Code).where(Code.status == CodeStatus.NONE, ~exists(select(RuleExclusion.code).where(RuleExclusion.code == Code.code)))
        for row in session.scalars(query).yield_per(500):
            for rule in rules:
                if not matches(row.to_dict(), rule.conditions):
                    continue
                # 按方案判断 VR，未指定方案时仍遵循全局过滤。
                profile = session.get(DownloadProfile, rule.profile_id) if rule.profile_id else None
                if profile:
                    blocked = profile.filters.get("exclude_vr") and has_vr(f"{row.code} {row.title or ''}")
                else:
                    blocked = bool(is_filtered_code(row.code, row.title or ""))
                if blocked:
                    continue
                changed = session.query(Code).filter(Code.code == row.code, Code.status == CodeStatus.NONE).update({Code.status: CodeStatus.SUBSCRIBED}, synchronize_session=False)
                if changed and profile:
                    session.merge(CodeProfile(code=row.code, profile_id=profile.id))
                count += changed
                break
    return count
