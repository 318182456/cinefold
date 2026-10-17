"""订阅方案、条件订阅与收藏管理 API。"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.api.endpoints import get_current_user
from app.database.models import Code, CodeProfile, Collection, CollectionItem, DownloadProfile, LibraryPreference, SubscriptionRule
from app.database.session import session_scope
from app.schemas.reponse import ResponseEntity
from app.services.rules import preview_rule, run_rules, validate_conditions

router = APIRouter(tags=["curation"], dependencies=[Depends(get_current_user)])


class TorrentFilters(BaseModel):
    model_config = {"extra": "forbid"}
    only_chinese: bool = False
    only_uc: bool = False
    exclude_uc: bool = False
    only_uhd: bool = False
    exclude_uhd: bool = False
    only_vr: bool = False
    exclude_vr: bool = False
    only_free: bool = False
    include_keywords: str = Field(default="", max_length=500)
    exclude_keywords: str = Field(default="", max_length=500)
    min_size: str = ""
    max_size: str = ""
    min_seeders: int = Field(default=0, ge=0)
    max_seeders: int = Field(default=0, ge=0)


class ProfileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    filters: TorrentFilters = Field(default_factory=TorrentFilters)
    sort: str = Field(default="free,chinese,seeders", max_length=255)


class RuleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = False
    conditions: dict = Field(default_factory=dict)
    profile_id: int | None = None


def checked_conditions(conditions: dict, allow_empty: bool = False) -> dict:
    try:
        return validate_conditions(conditions, allow_empty=allow_empty)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


def checked_profile(session, profile_id):
    if profile_id is not None and session.get(DownloadProfile, profile_id) is None:
        raise HTTPException(404, "下载方案不存在")


@router.get("/profiles")
def list_profiles():
    with session_scope() as session:
        return ResponseEntity.ok([p.to_dict() for p in session.scalars(select(DownloadProfile).order_by(DownloadProfile.id))])


@router.post("/profiles")
def create_profile(body: ProfileRequest):
    return save_profile(body)


@router.put("/profiles/{profile_id}")
def update_profile(profile_id: int, body: ProfileRequest):
    return save_profile(body, profile_id)


def save_profile(body, profile_id=None):
    import re
    from app.utils.filters import SIZE_RE, _parse_size_mb

    if not body.name.strip():
        raise HTTPException(422, "方案名称不能为空")
    filters = body.filters.model_dump()
    for key in ("min_size", "max_size"):
        if filters[key] and not SIZE_RE.fullmatch(filters[key]):
            raise HTTPException(422, "体积请填写 5GB 或 500MB")
    if filters["max_size"] and _parse_size_mb(filters["min_size"]) > _parse_size_mb(filters["max_size"]):
        raise HTTPException(422, "最小体积不能超过最大体积")
    if filters["max_seeders"] and filters["min_seeders"] > filters["max_seeders"]:
        raise HTTPException(422, "最小做种数不能超过最大做种数")
    for key in ("uc", "uhd", "vr"):
        if filters[f"only_{key}"] and filters[f"exclude_{key}"]:
            raise HTTPException(422, f"{key} 不能同时要求和排除")
    if any(not re.fullmatch(r"!?(free|chinese|uc|uhd|vr|seeders|size|site)", s.strip()) for s in body.sort.split(",") if s.strip()):
        raise HTTPException(422, "排序包含未知字段")
    with session_scope() as session:
        row = session.get(DownloadProfile, profile_id) if profile_id else DownloadProfile()
        if row is None:
            raise HTTPException(404, "下载方案不存在")
        row.name, row.filters, row.sort = body.name.strip(), filters, body.sort
        session.add(row)
        session.flush()
        return ResponseEntity.ok(row.to_dict())


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: int):
    with session_scope() as session:
        if session.scalar(select(SubscriptionRule.id).where(SubscriptionRule.profile_id == profile_id).limit(1)) or session.scalar(select(CodeProfile.code).where(CodeProfile.profile_id == profile_id).limit(1)):
            raise HTTPException(409, "该方案正在被订阅使用，请先解除关联")
        session.execute(delete(DownloadProfile).where(DownloadProfile.id == profile_id))
    return ResponseEntity.ok()


class AssignmentRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    profile_id: int | None = None


@router.post("/profiles/assign")
def assign_profile(body: AssignmentRequest):
    from app.utils import get_true_code
    code = get_true_code(body.code) or body.code.strip()
    with session_scope() as session:
        if session.get(Code, code) is None:
            raise HTTPException(404, "番号不存在，请先搜索或订阅")
        checked_profile(session, body.profile_id)
        if body.profile_id is None:
            session.execute(delete(CodeProfile).where(CodeProfile.code == code))
        else:
            session.merge(CodeProfile(code=code, profile_id=body.profile_id))
    return ResponseEntity.ok()


@router.get("/subscription-rules")
def list_rules():
    with session_scope() as session:
        return ResponseEntity.ok([r.to_dict() for r in session.scalars(select(SubscriptionRule).order_by(SubscriptionRule.id))])


@router.post("/subscription-rules/preview")
def preview(body: RuleRequest):
    return ResponseEntity.ok(preview_rule(checked_conditions(body.conditions)))


@router.post("/subscription-rules/run")
def run():
    return ResponseEntity.ok({"subscribed": run_rules()})


@router.post("/subscription-rules")
def create_rule(body: RuleRequest):
    return save_rule(body)


@router.put("/subscription-rules/{rule_id}")
def update_rule(rule_id: int, body: RuleRequest):
    return save_rule(body, rule_id)


def save_rule(body, rule_id=None):
    conditions = checked_conditions(body.conditions)
    if not body.name.strip():
        raise HTTPException(422, "规则名称不能为空")
    with session_scope() as session:
        checked_profile(session, body.profile_id)
        row = session.get(SubscriptionRule, rule_id) if rule_id else SubscriptionRule()
        if row is None:
            raise HTTPException(404, "规则不存在")
        row.name, row.enabled, row.conditions, row.profile_id = body.name.strip(), body.enabled, conditions, body.profile_id
        session.add(row)
        session.flush()
        return ResponseEntity.ok(row.to_dict())


@router.delete("/subscription-rules/{rule_id}")
def delete_rule(rule_id: int):
    with session_scope() as session:
        session.execute(delete(SubscriptionRule).where(SubscriptionRule.id == rule_id))
    return ResponseEntity.ok()


@router.get("/library")
def library(keyword: str = "", genre: str = "", actor: str = "", publisher: str = "", series: str = "",
            emby_collection_key: str = Query('',max_length=64),
            system_collection: str = Query('', max_length=40),
            collection_id: int | None = None, favorite: bool = False, emby_favorite: bool = False, watch_later: bool = False,
            unseen: bool = False, include_skipped: bool = False,
            sort: Literal["recent", "release", "rating", "plays", "size"] = "recent",
            page: int = Query(1, ge=1), size: int = Query(30, ge=1, le=100)):
    from app.services.library import library_items
    conditions = {k: v for k, v in {"genres": [genre] if genre else [], "casts": [actor] if actor else [], "publisher": publisher, "series": series}.items() if v}
    try:
        return ResponseEntity.ok(library_items(keyword=keyword, conditions=conditions, collection_id=collection_id,
            emby_collection_key=emby_collection_key,
            system_collection=system_collection,
            favorite=favorite, emby_favorite=emby_favorite, watch_later=watch_later, unseen=unseen, sort=sort, page=page, size=size, include_skipped=include_skipped))
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/library/stats")
def library_stats(year: int | None = Query(None, ge=1900, le=2200)):
    from app.services.library import library_stats
    return ResponseEntity.ok(library_stats(year))


@router.get("/playback")
def playback_report(month: str = Query(..., pattern=r"^(?:19|20|21)\d{2}-(?:0[1-9]|1[0-2])$"),
                    user_id: str = "", day: str = "", page: int = Query(1, ge=1), size: int = Query(30, ge=1, le=100)):
    if day:
        from datetime import date
        try:
            if not day.startswith(month + "-"):
                raise ValueError
            date.fromisoformat(day)
        except ValueError:
            raise HTTPException(422, "日期必须属于所选月份")
    from app.services.playback import report
    return ResponseEntity.ok(report(month, user_id, day, page, size))


@router.post("/playback/poll")
def poll_playback():
    from app.services.playback import poll
    return ResponseEntity.ok({"observed": poll()})


@router.get("/library/{code}/similar")
def similar(code: str):
    from app.services.library import similar
    try:
        return ResponseEntity.ok(similar(code))
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


class PreferenceRequest(BaseModel):
    favorite: bool | None = None
    watch_later: bool | None = None
    skipped: bool | None = None
    rating: int | None = Field(default=None, ge=0, le=10)
    notes: str | None = Field(default=None, max_length=5000)


@router.patch("/library/{code}/preference")
def preference(code: str, body: PreferenceRequest):
    with session_scope() as session:
        if session.get(Code, code) is None:
            raise HTTPException(404, "番号不存在")
        row = session.get(LibraryPreference, code) or LibraryPreference(code=code)
        for key, value in body.model_dump(exclude_unset=True).items():
            if value is None and key != "rating":
                raise HTTPException(422, f"{key} 不能为空")
            setattr(row, key, value)
        session.add(row)
        session.flush()
        return ResponseEntity.ok(row.to_dict())


class CollectionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: Literal["manual", "smart"] = "manual"
    conditions: dict = Field(default_factory=dict)


@router.get("/collections")
def collections():
    with session_scope() as session:
        return ResponseEntity.ok([r.to_dict() for r in session.scalars(select(Collection).order_by(Collection.id))])


@router.get('/collections/system')
def system_collections():
    from app.services.playlists import SYSTEM_PLAYLISTS
    return ResponseEntity.ok(SYSTEM_PLAYLISTS)


@router.post("/collections")
def create_collection(body: CollectionRequest):
    return save_collection(body)


@router.put("/collections/{collection_id}")
def update_collection(collection_id: int, body: CollectionRequest):
    return save_collection(body, collection_id)


def save_collection(body, collection_id=None):
    from app.services.playlists import validate
    try:
        conditions = validate(body.conditions) if body.kind == 'smart' else checked_conditions(body.conditions, allow_empty=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if not body.name.strip():
        raise HTTPException(422, "片单名称不能为空")
    with session_scope() as session:
        row = session.get(Collection, collection_id) if collection_id else Collection()
        if row is None:
            raise HTTPException(404, "片单不存在")
        if collection_id and row.kind != body.kind:
            raise HTTPException(422, "请创建新片单来更换片单类型")
        row.name, row.kind, row.conditions = body.name.strip(), body.kind, conditions
        session.add(row)
        session.flush()
        return ResponseEntity.ok(row.to_dict())


@router.delete("/collections/{collection_id}")
def delete_collection(collection_id: int):
    with session_scope() as session:
        session.execute(delete(CollectionItem).where(CollectionItem.collection_id == collection_id))
        session.execute(delete(Collection).where(Collection.id == collection_id))
    return ResponseEntity.ok()


class CollectionCodes(BaseModel):
    codes: list[str] = Field(min_length=1, max_length=200)


@router.post("/collections/{collection_id}/items")
def add_items(collection_id: int, body: CollectionCodes):
    with session_scope() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(404, "片单不存在")
        if collection.kind != "manual":
            raise HTTPException(422, "智能片单按条件自动收录")
        codes = set(body.codes)
        existing = set(session.scalars(select(Code.code).where(Code.code.in_(codes))))
        if codes - existing:
            raise HTTPException(404, "部分番号不存在")
        for code in sorted(codes):
            session.merge(CollectionItem(collection_id=collection_id, code=code))
    return ResponseEntity.ok()


@router.delete("/collections/{collection_id}/items/{code}")
def remove_item(collection_id: int, code: str):
    with session_scope() as session:
        session.execute(delete(CollectionItem).where(CollectionItem.collection_id == collection_id, CollectionItem.code == code))
    return ResponseEntity.ok()


@router.get("/library/emby/users")
def emby_users():
    from app.services.embysync import users
    try:
        return ResponseEntity.ok(users())
    except Exception:
        raise HTTPException(400, "读取 Emby 用户失败，请检查地址、API Key 和网络")


@router.post("/library/emby/sync")
def emby_sync():
    from app.services.embysync import sync
    try:
        return ResponseEntity.ok(sync())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        raise HTTPException(502, "Emby 同步失败，旧数据已保留，请检查服务连接")


class EmbyActionRequest(BaseModel):
    item_id: str = Field(min_length=1, max_length=128)
    action: Literal['favorite', 'unfavorite', 'played', 'unplayed']


@router.post("/library/{code}/emby")
def emby_action(code: str, body: EmbyActionRequest):
    from app.services.embysync import set_user_state
    try:
        return ResponseEntity.ok(set_user_state(code, body.item_id, body.action))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        raise HTTPException(502, "Emby 操作结果未确认，请检查连接并重新同步")


class HealthRequest(BaseModel):
    hashes: bool = False


class CloudRequest(BaseModel):
    magnet: str = Field(min_length=1, max_length=16000)
    save_path: str = Field(default="", max_length=1000)


@router.post("/cloudnas/offline")
def cloud_offline(body: CloudRequest):
    from app.modules.cloudnas import add_offline
    try:
        return ResponseEntity.ok(add_offline(body.magnet, body.save_path))
    except ImportError:
        raise HTTPException(400, "CloudDrive 依赖未安装，请更新 requirements.txt 中的依赖后重试")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        raise HTTPException(502, "CloudDrive 请求失败，请检查服务地址、端口与网络")


@router.get("/library/health/status")
def health_status():
    from app.services.libraryhealth import snapshot
    return ResponseEntity.ok(snapshot())


@router.post("/library/health/scan")
def scan_health(body: HealthRequest):
    from app.services.libraryhealth import start
    return ResponseEntity.ok(start(hashes=body.hashes))
