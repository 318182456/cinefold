"""演员订阅。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.endpoints import get_current_user
from app.database.models import Actor
from app.database.session import session_scope
from app.schemas.reponse import ResponseEntity

router = APIRouter(tags=["actors"])


class ActorRequest(BaseModel):
    name: str
    limit_date: str | None = None


class ActorRefreshRequest(BaseModel):
    name: str
    max_pages: int = 3


class ActorPhotosRequest(BaseModel):
    names: list[str] = Field(min_length=1, max_length=24)
    force: bool = False


@router.post('/actors/photos')
def actor_photos(body: ActorPhotosRequest, current_user: str = Depends(get_current_user)):
    from app.services.actorphotos import start
    return ResponseEntity.ok(start(body.names, body.force))


@router.get('/actors/photos/status')
def actor_photos_status(current_user: str = Depends(get_current_user)):
    from app.services.actorphotos import current
    return ResponseEntity.ok(current())


@router.get('/actors/profile')
def actor_profile(name: str, page: int = 1, size: int = 24, filter: str = 'all', current_user: str = Depends(get_current_user)):
    from app.services.actorprofile import profile
    try:
        return ResponseEntity.ok(profile(name, page=page, size=size, filter=filter))
    except ValueError as exc:
        return ResponseEntity.fail(str(exc), code=400)


@router.post('/actors/profile/refresh')
def refresh_actor_profile(body: ActorRefreshRequest, current_user: str = Depends(get_current_user)):
    from app.services.actorprofile import refresh
    try:
        return ResponseEntity.ok(refresh(body.name, body.max_pages))
    except ValueError as exc:
        return ResponseEntity.fail(str(exc), code=400)


@router.get('/actors/profile/sync')
def actor_profile_sync(name: str, current_user: str = Depends(get_current_user)):
    from app.services.actorprofile import sync_state
    return ResponseEntity.ok(sync_state(name))


@router.post('/actors/profile/subscribe')
def subscribe_actor_works(body: ActorRequest, current_user: str = Depends(get_current_user)):
    from app.services.actorprofile import subscribe_works
    try:
        return ResponseEntity.ok(subscribe_works(body.name))
    except ValueError as exc:
        return ResponseEntity.fail(str(exc), code=400)


@router.get("/actors")
def list_actors(
    keyword: str = "",
    page: int = 1,
    size: int = 15,
    subscribed: bool = True,
    current_user: str = Depends(get_current_user),
):
    """演员列表。

    默认只列已订阅的。actor 表里还有爬虫库导入的资料缓存，那些不是订阅，
    列出来会让人以为自己订了上千个演员；subscribed=false 可查全表。
    """
    from app.services.actorprofile import directory
    return ResponseEntity.ok(directory(keyword=keyword, page=page, size=size, subscribed=subscribed))


@router.post("/actors/sub")
def subscribe(body: ActorRequest, current_user: str = Depends(get_current_user)):
    from app import services
    services.subscribe_actor(body.name, body.limit_date or "")
    return ResponseEntity.ok(message=f"已订阅演员 {body.name}")


@router.post("/actors/cancel")
def cancel(body: ActorRequest, current_user: str = Depends(get_current_user)):
    from app import services
    # 取消只翻标记不删行，所以失败等于「本来就没订阅」
    if not services.cancel_actor(body.name):
        return ResponseEntity.fail("该演员未在订阅中", code=404)
    return ResponseEntity.ok(message=f"已取消订阅 {body.name}")


@router.get("/actors/rank")
def rank(limit: int = 30, current_user: str = Depends(get_current_user)):
    """按作品数排序的演员榜。"""
    try:
        from app.modules import ladysite
        items = ladysite.get_actor_rank()
        if items:
            return ResponseEntity.ok({"items": items})
    except (ImportError, AttributeError):
        pass

    # 抓不到榜单时拿本地演员兜底。这里要的是「有哪些演员」，
    # 资料缓存同样算数，所以不按 subscribed 筛
    with session_scope() as session:
        rows = session.scalars(select(Actor).limit(limit)).all()
        return ResponseEntity.ok({"items": [row.to_dict() for row in rows]})


@router.get("/actors/codes")
def actor_codes(name: str, limit: int = 50, current_user: str = Depends(get_current_user)):
    """某演员的作品列表。"""
    from app.services.actorprofile import profile
    try:
        return ResponseEntity.ok({'items': profile(name, size=limit)['items']})
    except ValueError as exc:
        return ResponseEntity.fail(str(exc), code=400)
