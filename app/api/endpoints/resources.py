"""资源源和本地磁链索引。"""
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select

from app.api.endpoints import get_current_user
from app.database.models import Resource, ResourceFeed, ResourceMatch
from app.database.session import session_scope
from app.schemas.reponse import ResponseEntity

router = APIRouter(prefix="/resources", tags=["resources"], dependencies=[Depends(get_current_user)])


class FeedRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=4000)
    kind: Literal["json", "rss", "forum"] = "json"
    enabled: bool = False
    cookie: str | None = Field(default=None, max_length=16000)


def safe_feed(row):
    item = row.to_dict()
    item["has_cookie"] = bool(item.pop("cookie", ""))
    return item


@router.get("/feeds")
def feeds():
    with session_scope() as session:
        return ResponseEntity.ok([safe_feed(x) for x in session.scalars(select(ResourceFeed).order_by(ResourceFeed.id))])


@router.post("/feeds")
def create_feed(body: FeedRequest):
    return save_feed(body)


@router.put("/feeds/{feed_id}")
def update_feed(feed_id: int, body: FeedRequest):
    return save_feed(body, feed_id)


def save_feed(body, feed_id=None):
    parsed = urlsplit(body.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(422, "请填写 HTTP(S) 地址；登录信息请放在 Cookie 中")
    if not body.name.strip():
        raise HTTPException(422, "名称不能为空")
    with session_scope() as session:
        row = session.get(ResourceFeed, feed_id) if feed_id else ResourceFeed()
        if row is None:
            raise HTTPException(404, "资源源不存在")
        row.name, row.url, row.kind, row.enabled = body.name.strip(), body.url, body.kind, body.enabled
        if body.cookie is not None:
            row.cookie = body.cookie.replace("\r", "").replace("\n", "").strip()
        session.add(row); session.flush()
        return ResponseEntity.ok(safe_feed(row))


@router.delete("/feeds/{feed_id}")
def delete_feed(feed_id: int):
    with session_scope() as session:
        session.execute(delete(ResourceFeed).where(ResourceFeed.id == feed_id))
    return ResponseEntity.ok()


@router.post("/collect")
def collect():
    from app.services.resources import start
    return ResponseEntity.ok(start())


@router.get("/status")
def status():
    from app.services.resources import state
    return ResponseEntity.ok(state())


class ImportRequest(BaseModel):
    source: str = Field(default="手工导入", min_length=1, max_length=128)
    items: list[dict] = Field(min_length=1, max_length=2000)


@router.post("/import")
def import_resources(body: ImportRequest):
    from app.services.resources import import_items
    return ResponseEntity.ok({"added": import_items(body.items, body.source)})


@router.get("")
def list_resources(keyword: str = "", page: int = Query(1, ge=1), size: int = Query(30, ge=1, le=100)):
    with session_scope() as session:
        query = select(Resource)
        if keyword:
            query = query.where(or_(Resource.title.ilike(f"%{keyword}%"), Resource.hash.in_(select(ResourceMatch.hash).where(ResourceMatch.code == keyword.upper()))))
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = session.scalars(query.order_by(Resource.create_time.desc()).offset((page - 1) * size).limit(size)).all()
        return ResponseEntity.ok({"items": [x.to_dict() for x in rows], "total": total})


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
