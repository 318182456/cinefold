"""番号订阅、搜索、下载。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from app.api.endpoints import get_current_user
from app.database.models import Code, CodeStatus
from app.database.session import session_scope
from app.schemas.reponse import ResponseEntity
from app.schemas.torrent import Torrent
from app.utils import get_true_code

router = APIRouter(tags=["subscribe"])


class SubscribeRequest(BaseModel):
    code: str


class DownloadRequest(BaseModel):
    code: str
    download_url: str | None = None
    site: str | None = None


@router.get("/dashboard")
def dashboard(current_user: str = Depends(get_current_user)):
    from app import services
    return ResponseEntity.ok(services.dashboard_stats())


@router.get("/codes/list")
def list_code(
    status: int | None = None,
    page: int = 1,
    size: int = 30,
    current_user: str = Depends(get_current_user),
):
    """番号列表，可按状态筛选。"""
    from sqlalchemy import func

    with session_scope() as session:
        query = select(Code)
        count_query = select(func.count()).select_from(Code)
        if status is not None:
            query = query.where(Code.status == status)
            count_query = count_query.where(Code.status == status)

        total = session.scalar(count_query) or 0
        rows = session.scalars(
            query.order_by(Code.update_time.desc())
            .offset(max(page - 1, 0) * size)
            .limit(size)
        ).all()

        return ResponseEntity.ok({
            "total": total,
            "page": page,
            "size": size,
            "items": [row.to_dict() for row in rows],
        })


@router.get("/search")
def search_codes(keyword: str, current_user: str = Depends(get_current_user)):
    """搜番号。本地库无结果时尝试远程站点。"""
    from app import services

    code = get_true_code(keyword)
    local = services.search_code(keyword)
    if local:
        return ResponseEntity.ok({"items": local, "source": "local"})

    try:
        from app.modules import ladysite
        remote = ladysite.search_code(code or keyword)
        # 抓回来的情报存进库，下次同一个番号直接走本地
        services.cache_remote_codes(remote)
        return ResponseEntity.ok({"items": remote, "source": "remote"})
    except (ImportError, AttributeError):
        return ResponseEntity.ok({"items": [], "source": "local"})


@router.get("/torrents")
def search_torrents(code: str, current_user: str = Depends(get_current_user)):
    """搜索番号对应的种子。"""
    from app import services
    torrents = services.search_torrents(get_true_code(code) or code)
    return ResponseEntity.ok({"items": [t.to_dict() for t in torrents]})


@router.post("/codes/sub")
def sub(body: SubscribeRequest, current_user: str = Depends(get_current_user)):
    from app import services
    code = get_true_code(body.code) or body.code
    services.subscribe_code(code)
    return ResponseEntity.ok(message=f"已订阅 {code}")


@router.post("/codes/cancel")
def cancel(body: SubscribeRequest, current_user: str = Depends(get_current_user)):
    from app import services
    code = get_true_code(body.code) or body.code
    if not services.cancel_subscribe(code):
        return ResponseEntity.fail("番号不存在", code=404)
    return ResponseEntity.ok(message=f"已取消订阅 {code}")


@router.post("/codes/download")
def manual_download(body: DownloadRequest, current_user: str = Depends(get_current_user)):
    """手动下载。指定 download_url 时直接推送该种子。"""
    from app import services

    code = get_true_code(body.code) or body.code
    torrent = None
    if body.download_url:
        torrent = Torrent(
            site=body.site or "manual",
            title=code,
            download_url=body.download_url,
            code=code,
        )

    if not services.download_torrent(code, torrent):
        return ResponseEntity.fail("下载失败，请查看日志", code=500)
    return ResponseEntity.ok(message=f"已推送 {code} 到下载器")


@router.post("/codes/download/all")
def download_subscribe(current_user: str = Depends(get_current_user)):
    """立即执行一轮订阅下载。"""
    from app.scheduler import push_job
    push_job("run_codes_task")
    return ResponseEntity.ok(message="订阅下载任务已触发")


@router.get("/codes/release_today")
def release_today(current_user: str = Depends(get_current_user)):
    """今日发行的番号。"""
    today = date.today().strftime("%Y-%m-%d")
    with session_scope() as session:
        rows = session.scalars(
            select(Code).where(Code.release_date == today).limit(100)
        ).all()
        return ResponseEntity.ok({"items": [row.to_dict() for row in rows]})


@router.get("/codes/recommend")
def recommend(limit: int = 20, current_user: str = Depends(get_current_user)):
    """推荐：评分高且未订阅的番号。"""
    with session_scope() as session:
        rows = session.scalars(
            select(Code)
            .where(Code.status == CodeStatus.NONE, Code.star.isnot(None))
            .order_by(Code.star.desc())
            .limit(limit)
        ).all()
        return ResponseEntity.ok({"items": [row.to_dict() for row in rows]})


@router.get("/rank")
def rank(rank_type: str = "", current_user: str = Depends(get_current_user)):
    """榜单，带标题封面。抓不到时返回本地高分番号。"""
    from app import services

    items = services.get_rank_items(rank_type)
    if items:
        return ResponseEntity.ok({"items": items})

    return recommend(limit=30, current_user=current_user)


@router.post("/rank/subscribe")
def rank_subscribe(current_user: str = Depends(get_current_user)):
    from app.scheduler import push_job
    push_job("sub_rank")
    return ResponseEntity.ok(message="榜单订阅任务已触发")


@router.get("/hot")
def hot(current_user: str = Depends(get_current_user)):
    """热门。"""
    return recommend(limit=30, current_user=current_user)


@router.get("/brands")
def brands(current_user: str = Depends(get_current_user)):
    """可抓取的厂牌列表，附带库中已出现过的发行商。"""
    from app.modules.ladysite.brands import BRAND_LABELS, BRANDS

    with session_scope() as session:
        rows = session.execute(
            select(Code.publisher)
            .where(Code.publisher.isnot(None), Code.publisher != "")
            .distinct()
            .limit(100)
        ).all()

    return ResponseEntity.ok({
        # 支持按日期抓新片/预定发布的厂牌
        "brands": [
            {"key": key, "label": BRAND_LABELS.get(key, key)} for key in BRANDS
        ],
        # 库里已有的发行商，仅用于展示
        "publishers": [row[0] for row in rows],
    })


@router.get("/brands/codes")
def brand_codes(
    brand: str,
    past_days: int = 7,
    future_days: int = 14,
    current_user: str = Depends(get_current_user),
):
    """某厂牌的最新发布与预定发布作品。"""
    from app import services
    from app.modules.ladysite.brands import BRANDS

    key = (brand or "").strip().lower()
    if key not in BRANDS:
        return ResponseEntity.fail(f"未知厂牌 {brand}", code=400)

    items = services.get_brand_items(
        key,
        past_days=max(0, min(past_days, 60)),
        future_days=max(0, min(future_days, 90)),
    )
    return ResponseEntity.ok({"items": items})


@router.post("/codes/translate")
def translate_rank_title(current_user: str = Depends(get_current_user)):
    from app.scheduler import push_job
    push_job("translate_titles")
    return ResponseEntity.ok(message="翻译任务已触发")
