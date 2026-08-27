"""番号订阅、搜索、下载。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select

from app.api.endpoints import get_current_user
from app.database.models import Code, CodeStatus
from app.database.session import session_scope
from app.schemas.reponse import ResponseEntity
from app.schemas.torrent import Torrent
from app.utils import get_true_code

router = APIRouter(tags=["subscribe"])

# 列表页与榜单每页返回多少条。每条都带封面，太多会拖慢首屏
PAGE_SIZE = 15


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
    size: int = PAGE_SIZE,
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

        # 资源站搜不到时未必老实报空 —— 有的返回相近番号，有的直接给搜索页
        # 第一条（搜 SONS-183 拿回 NASK-183 就是这么来的）。番号是精确标识，
        # 对不上就是没有，不能拿去糊弄用户，更不能落库污染本地情报。
        #
        # 只在请求方给出了合法番号时才校验：按标题、演员名之类的关键词搜索
        # 本就不该要求返回值等于关键词
        if code:
            matched = [
                item for item in remote
                if (get_true_code(item.get("code", "")) or "").upper() == code.upper()
            ]
            if len(matched) != len(remote):
                dropped = [item.get("code", "") for item in remote if item not in matched]
                logger.info(f"[{code}] 远程返回 {dropped} 与请求番号不符，已丢弃")
            remote = matched

        # 抓回来的情报存进库，下次同一个番号直接走本地
        services.cache_remote_codes(remote)
        return ResponseEntity.ok({"items": remote, "source": "remote"})
    except (ImportError, AttributeError):
        return ResponseEntity.ok({"items": [], "source": "local"})


@router.get("/torrents")
def search_torrents(
    code: str, refresh: bool = False, current_user: str = Depends(get_current_user)
):
    """搜索番号对应的种子。refresh=true 跳过缓存强制重搜。

    不套过滤规则：那套条件是给自动选种用的（"该下哪个"），而人工检索
    问的是"站上有什么"。拿订阅口味把列表筛空，用户只会对着空页面猜
    是没资源还是配置太严 —— 挑哪个种子，让他自己看着列表决定。
    """
    from app import services
    torrents = services.search_torrents(
        get_true_code(code) or code, use_filter=False, refresh=refresh
    )
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


class ResetRequest(BaseModel):
    code: str
    # 默认只预览。这个操作会删下载记录并重置状态，先让用户看清再落库
    dry_run: bool = True


@router.post("/codes/reset")
def reset_code(body: ResetRequest, current_user: str = Depends(get_current_user)):
    """把番号恢复到「可以重新下载」的状态。

    用于文件已经删干净、却一直报「已存在」下不下来的番号。三处残留任一
    存在都会拦住它：媒体库判定缓存、History 下载记录、卡住的 Code.status。
    这里一次清掉，具体见 services.reset_code_for_redownload。

    只清下载器里确实已经没有的 History 行；种子还在做种的照旧保留。
    """
    from app import services

    code = get_true_code(body.code) or body.code
    if not code:
        return ResponseEntity.fail("番号为空", code=400)

    result = services.reset_code_for_redownload(code, dry_run=body.dry_run)
    if result.get("error"):
        return ResponseEntity.fail(result["error"], code=400)

    if body.dry_run:
        return ResponseEntity.ok(result, message="预览：以下内容将被清理")

    if result.get("downloader_unavailable"):
        return ResponseEntity.ok(
            result,
            message=(
                f"{code} 已清缓存，但下载器连不上，"
                f"保留了 {len(result['history_kept'])} 条下载记录未动"
            ),
        )

    return ResponseEntity.ok(result, message=f"{code} 已可重新下载")


@router.post("/cache/media/clear")
def clear_media_cache(current_user: str = Depends(get_current_user)):
    """清空「番号是否已入库」的全部判定缓存。

    这个缓存曾经写进 Redis 时没设过期时间，存量 key 不会自己消失。
    整库判定都不准时用它一次清干净，代价只是下次查询要重新问一遍媒体库。
    """
    from app import services

    services.drop_media_exists_cache()
    return ResponseEntity.ok(message="已清空媒体库判定缓存")


class CodesRequest(BaseModel):
    codes: list[str] = []


@router.post("/codes/sub/batch")
def sub_batch(body: CodesRequest, current_user: str = Depends(get_current_user)):
    """按选中的番号批量订阅。"""
    from app import services

    codes = [get_true_code(c) or c for c in body.codes if c and c.strip()]
    if not codes:
        return ResponseEntity.fail("没有选中任何番号", code=400)

    result = services.subscribe_codes(codes)
    done = len(result["subscribed"])
    skipped = len(result["filtered"])
    message = f"已订阅 {done} 个"
    if skipped:
        message += f"，{skipped} 个被过滤规则拦下"
    return ResponseEntity.ok(result, message=message)


@router.post("/codes/cancel/batch")
def cancel_batch(body: CodesRequest, current_user: str = Depends(get_current_user)):
    """按选中的番号批量取消订阅。"""
    from app import services

    codes = [get_true_code(c) or c for c in body.codes if c and c.strip()]
    if not codes:
        return ResponseEntity.fail("没有选中任何番号", code=400)

    result = services.cancel_subscribe_codes(codes)
    done = len(result["cancelled"])
    missing = len(result["missing"])
    message = f"已取消订阅 {done} 个"
    if missing:
        message += f"，{missing} 个不在库中"
    return ResponseEntity.ok(result, message=message)


class BulkCancelRequest(BaseModel):
    # 只取消这个日期之前发行的
    before_date: str = ""
    # 或者：保留最近多少天的，更早的取消
    keep_recent_days: int = 0
    # 只取消 VR
    only_vr: bool = False
    # 默认只试算，要真的执行得显式传 False
    dry_run: bool = True


@router.post("/codes/cancel/bulk")
def bulk_cancel(
    body: BulkCancelRequest, current_user: str = Depends(get_current_user)
):
    """批量取消订阅。默认试算，返回命中数量与样本。"""
    from app import services

    if not (body.before_date or body.keep_recent_days or body.only_vr):
        return ResponseEntity.fail(
            "至少指定一个条件，避免误清空整个订阅列表", code=400
        )

    result = services.bulk_cancel_subscribe(
        before_date=body.before_date,
        only_vr=body.only_vr,
        keep_recent_days=body.keep_recent_days,
        dry_run=body.dry_run,
    )
    action = "试算" if result["dry_run"] else "已取消"
    return ResponseEntity.ok(result, message=f"{action} {result['matched']} 个订阅")


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
def recommend(
    limit: int = PAGE_SIZE,
    page: int = 1,
    current_user: str = Depends(get_current_user),
):
    """推荐：评分高且未订阅的番号。

    高分未订阅的番号可能有上千条，一次全给前端太多，按 page 翻页。
    榜单退化调用只关心前 limit 条，不传 page 时行为不变。
    """
    from sqlalchemy import func

    size = max(limit, 1)
    page = max(page, 1)
    # 评分相同时按番号定序，否则翻页会出现重复或漏项
    conditions = (Code.status == CodeStatus.NONE, Code.star.isnot(None))
    with session_scope() as session:
        total = session.scalar(
            select(func.count()).select_from(Code).where(*conditions)
        ) or 0
        rows = session.scalars(
            select(Code)
            .where(*conditions)
            .order_by(Code.star.desc(), Code.code)
            .offset((page - 1) * size)
            .limit(size)
        ).all()
        return ResponseEntity.ok({
            "items": [row.to_dict() for row in rows],
            "total": total,
            "page": page,
            "size": size,
        })


@router.get("/rank")
def rank(
    rank_type: str = "",
    limit: int = PAGE_SIZE,
    current_user: str = Depends(get_current_user),
):
    """榜单，带标题封面。抓不到时返回本地高分番号。"""
    from app import services

    items = services.get_rank_items(rank_type)
    if items:
        return ResponseEntity.ok({"items": items[:max(limit, 1)]})

    return recommend(limit=limit, current_user=current_user)


@router.post("/rank/subscribe")
def rank_subscribe(current_user: str = Depends(get_current_user)):
    from app.scheduler import push_job
    push_job("sub_rank")
    return ResponseEntity.ok(message="榜单订阅任务已触发")


@router.get("/hot")
def hot(limit: int = PAGE_SIZE, current_user: str = Depends(get_current_user)):
    """热门。"""
    return recommend(limit=limit, current_user=current_user)


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

    from app.modules.ladysite.brands import BrandUnreachable

    try:
        items = services.get_brand_items(
            key,
            past_days=max(0, min(past_days, 60)),
            future_days=max(0, min(future_days, 90)),
        )
    except BrandUnreachable as exc:
        # 空列表会被前端显示成"没有作品"，站点不通得说清楚
        return ResponseEntity.fail(str(exc), code=502)
    return ResponseEntity.ok({"items": items})


@router.post("/codes/translate")
def translate_rank_title(current_user: str = Depends(get_current_user)):
    from app.scheduler import push_job
    push_job("translate_titles")
    return ResponseEntity.ok(message="翻译任务已触发")
