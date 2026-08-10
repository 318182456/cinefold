"""演员订阅。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.endpoints import get_current_user
from app.database.models import Actor, Code
from app.database.session import session_scope
from app.schemas.reponse import ResponseEntity

router = APIRouter(tags=["actors"])


class ActorRequest(BaseModel):
    name: str
    limit_date: str | None = None


@router.get("/actors")
def list_actors(
    keyword: str = "",
    page: int = 1,
    size: int = 30,
    current_user: str = Depends(get_current_user),
):
    with session_scope() as session:
        query = select(Actor)
        count_query = select(func.count()).select_from(Actor)
        if keyword:
            pattern = f"%{keyword}%"
            condition = Actor.name.like(pattern) | Actor.name_2.like(pattern)
            query = query.where(condition)
            count_query = count_query.where(condition)

        total = session.scalar(count_query) or 0
        rows = session.scalars(
            query.order_by(Actor.update_time.desc())
            .offset(max(page - 1, 0) * size)
            .limit(size)
        ).all()

        return ResponseEntity.ok({
            "total": total,
            "items": [row.to_dict() for row in rows],
        })


@router.post("/actors/sub")
def subscribe(body: ActorRequest, current_user: str = Depends(get_current_user)):
    from app import services
    services.subscribe_actor(body.name, body.limit_date or "")
    return ResponseEntity.ok(message=f"已订阅演员 {body.name}")


@router.post("/actors/cancel")
def cancel(body: ActorRequest, current_user: str = Depends(get_current_user)):
    from app import services
    if not services.cancel_actor(body.name):
        return ResponseEntity.fail("演员不存在", code=404)
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

    with session_scope() as session:
        rows = session.scalars(select(Actor).limit(limit)).all()
        return ResponseEntity.ok({"items": [row.to_dict() for row in rows]})


@router.get("/actors/codes")
def actor_codes(name: str, limit: int = 50, current_user: str = Depends(get_current_user)):
    """某演员的作品列表。"""
    with session_scope() as session:
        rows = session.scalars(
            select(Code)
            .where(Code.casts.like(f"%{name}%"))
            .order_by(Code.release_date.desc())
            .limit(limit)
        ).all()
        return ResponseEntity.ok({"items": [row.to_dict() for row in rows]})
