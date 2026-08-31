"""硬链接关联管理。

webhook 登记的 media_link 记录在这里查看与维护。删除联动真的会删文件，
所以这个页面还兼作"删之前先看看会删什么"的入口 —— preview 走的正是
webhook 的同一条 dry_run 路径。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from app.api.endpoints import get_current_user
from app.core.config import get_settings
from app.database.models import CodeAlias, History, MediaLink, PendingDelete
from app.database.session import session_scope
from app.schemas.reponse import ResponseEntity
from app.services.medialink import (
    handle_media_deleted, register_scrape, torrent_batch,
)
from app.utils import get_true_code

router = APIRouter(prefix="/medialinks", tags=["硬链接"])


class RegisterRequest(BaseModel):
    code: str
    source_path: str
    link_path: str = ""


class DeleteRequest(BaseModel):
    """按 link_path 或 code 定位要清理的关联。"""
    link_path: str = ""
    code: str = ""
    dry_run: bool = True


class SubtitleRequest(BaseModel):
    code: str
    # 覆盖已有字幕。默认不覆盖，避免把用户手工放的那份冲掉
    force: bool = False


class ReviewRequest(BaseModel):
    code: str
    # 重新生成。默认不重生成，已有结果直接复用 —— 每次都是一次 AI 请求
    force: bool = False


class BatchRequest(BaseModel):
    """一批 link_path。孤儿一览的多选批量操作用。

    按 link_path 而不是 code：同一番号可能有多条链接，选中的是具体哪一条
    得由用户决定，按 code 会把没选中的那些也一起删了。
    """
    link_paths: list[str] = []
    dry_run: bool = True


def _exists(path: str) -> bool:
    """路径是否还在。跨容器挂载不一致时会误判为丢失，仅作提示用。"""
    try:
        return Path(path).exists()
    except OSError:
        return False


# 存在性探测的结果缓存。媒体库多半挂在 NAS 上，一次 exists() 就是一次网络
# 往返，几十条记录叠起来就是肉眼可见的卡顿。文件在不在这件事变化很慢，
# 缓存几十秒完全够用 —— 真删过文件的路径会被显式失效（见 _invalidate）。
_EXISTS_TTL = 30.0
_exists_cache: dict[str, tuple[float, bool]] = {}


def _exists_cached(path: str) -> bool:
    now = time.monotonic()
    hit = _exists_cache.get(path)
    if hit is not None and now - hit[0] < _EXISTS_TTL:
        return hit[1]
    value = _exists(path)
    _exists_cache[path] = (now, value)
    return value


def _invalidate(*paths: str) -> None:
    """删过文件之后把相关路径踢出缓存，避免页面还显示"存在"。"""
    for path in paths:
        _exists_cache.pop(path, None)
        _subtitle_cache.pop(path, None)



# 字幕探测要列一次目录，比 exists 贵，但同样是"看一眼磁盘"，沿用同样的
# TTL。抓完字幕后显式失效（见 fetch_subtitle），不必等 TTL 到期
_subtitle_cache: dict[str, tuple[float, bool]] = {}


def _has_subtitle_cached(path: str) -> bool:
    now = time.monotonic()
    hit = _subtitle_cache.get(path)
    if hit is not None and now - hit[0] < _EXISTS_TTL:
        return hit[1]

    from app.services.subtitle import has_subtitle

    value = has_subtitle(path)
    _subtitle_cache[path] = (now, value)
    return value


# 全量失效统计的缓存。stats 和 missing_only 都要扫全表做探测，
# 是这个页面最贵的一步，单独缓存一份共用。
# 探测本身是网络 IO，一轮下来分钟级都有可能，TTL 短了等于每次访问都重扫
_STATS_TTL = 600.0
_missing_cache: tuple[float, set[str]] | None = None

# 探测全是 IO 等待，并发跑能把 NAS 往返时间叠起来。不宜再高，
# 群晖这类设备并发请求太多反而会退化
_PROBE_WORKERS = 16

# 筛选前一次最多回填多少条。库特别大时不能让一次请求无限期地探下去 ——
# 超出的部分留给定时任务，页面上会提示「还有 N 条未统计」
_FILTER_BACKFILL_CAP = 5000


def _missing_links(force: bool = False) -> set[str]:
    """返回文件已丢失的 link_path 集合。

    全表探测，慢。结果缓存 _STATS_TTL 秒，列表页与概览共用同一份。
    """
    global _missing_cache

    now = time.monotonic()
    if not force and _missing_cache is not None and now - _missing_cache[0] < _STATS_TTL:
        return _missing_cache[1]

    with session_scope() as session:
        rows = session.execute(
            select(MediaLink.link_path, MediaLink.source_path)
        ).all()

    # 同一路径可能被多条记录共用（源文件对多个硬链接），去重后再探测
    paths = list({p for link, source in rows for p in (link, source)})
    with ThreadPoolExecutor(max_workers=_PROBE_WORKERS) as pool:
        probed = dict(zip(paths, pool.map(_exists_cached, paths)))

    missing = {
        link for link, source in rows
        if not probed[link] or not probed[source]
    }
    _missing_cache = (now, missing)
    return missing


def _total_size() -> dict:
    """媒体库里这些关联实际占了多少磁盘。

    大小已落库，这里是纯 SQL 聚合，不碰磁盘。

    硬链接与源文件共享同一份数据，同一部片子在分类目录里放了三份链接，
    三条记录的 size 都是同一个值 —— 直接 SUM 会算成三倍。所以按
    (device, inode) 去重后再累加，每份数据只算一次。

    inode 取不到时（跨文件系统、Windows 上 st_ino 为 0）退回按 link_path
    去重：宁可少算不重复算，这个数只用于估量级。
    """
    with session_scope() as session:
        rows = session.execute(
            select(
                MediaLink.link_path, MediaLink.inode,
                MediaLink.device, MediaLink.size,
            ).where(MediaLink.size.is_not(None))
        ).all()

    seen: set = set()
    total = 0
    for link, inode, device, size in rows:
        key = (device, inode) if inode else ("path", link)
        if key in seen:
            continue
        seen.add(key)
        total += size

    return {
        "bytes": total,
        # 去重后实际算进去的文件数。与关联总数的差额是共享 inode 的
        # 重复链接，加上还没探过大小的那些
        "files": len(seen),
    }


def _drop_stats_cache() -> None:
    """失效全部扫描缓存。删过文件或记录之后必须调，否则页面还显示旧结论。"""
    global _missing_cache, _orphan_cache
    _missing_cache = None
    _orphan_cache = None


def _attach_holds(items: list[dict], grace: int) -> None:
    """给列表项补上扣留信息：这条链接是否在等着被删、什么时候删。

    文件消失后不会立刻删，先扣留观察一个宽限期（见 PendingDelete）。页面上
    「文件已丢失」的那些记录，用户最想知道的就是它到底会不会删、还剩多久 ——
    只标红点说不清这件事。

    只查当前页的 link_path，扣留表通常很小，一次 IN 查询就够。
    """
    paths = [i["link_path"] for i in items]
    if not paths:
        return

    with session_scope() as session:
        holds = {
            r.link_path: r for r in session.scalars(
                select(PendingDelete).where(PendingDelete.link_path.in_(paths))
            ).all()
        }

    now = datetime.now()
    for item in items:
        row = holds.get(item["link_path"])
        if row is None:
            item["pending_delete"] = None
            continue
        deadline = row.detected_time + timedelta(seconds=grace)
        item["pending_delete"] = {
            # 哪一侧消失的，决定了到期后删的是源文件还是硬链接
            "side": row.side,
            "detected_time": row.detected_time.isoformat(),
            # 预计删除时刻。已过期的保留原值，前端据此显示「下轮对账将删除」
            "delete_at": deadline.isoformat(),
            "seconds_left": max(0, int((deadline - now).total_seconds())),
        }


def _backfill_rows(session, rows: list) -> None:
    """给当前页里还没探过的记录就地补上大小与字幕，并写回库。

    存量记录升级上来时这两列为空，等定时任务回填要等到下一轮。页面上
    该显示的东西不该受回填进度影响，所以看到空值就顺手探一次 ——
    只探眼前这一页，探完写库，翻回来时已是现成的。
    """
    now = datetime.now()
    # 同 inode 的多条链接共享一份数据，大小只探一次
    by_key: dict[tuple, int | None] = {}
    live = [r.link_path for r in rows if _exists_cached(r.link_path)]
    subtitled: dict[str, bool] = {}
    if live:
        with ThreadPoolExecutor(max_workers=_PROBE_WORKERS) as pool:
            subtitled = dict(zip(live, pool.map(_has_subtitle_cached, live)))

    for row in rows:
        key = (row.device, row.inode) if row.inode else ("path", row.link_path)
        if key not in by_key:
            target = (
                row.link_path if _exists_cached(row.link_path)
                else row.source_path
            )
            try:
                by_key[key] = Path(target).stat().st_size
            except OSError:
                by_key[key] = None
        row.size = by_key[key]
        row.size_probe_time = now
        row.has_subtitle = subtitled.get(row.link_path, False)


@router.get("")
def list_medialinks(
    keyword: str = "",
    missing_only: bool = False,
    sort: str = "time",
    min_gb: float = 0,
    max_gb: float = 0,
    subtitle: str = "",
    page: int = 1,
    size: int = 50,
    current_user: str = Depends(get_current_user),
):
    """关联列表。按番号分组返回，一个番号可能有多条硬链接。

    missing_only 只看文件已不存在的记录 —— 手工删过文件但没走 webhook 时，
    库里会留下这类孤儿记录。这一项需要全表探测文件是否存在，走缓存路径。

    sort 取 time / time_asc / code_asc / code_desc / size_desc / size_asc，
    默认 time（登记时间倒序，最新在前）。
    min_gb、max_gb 按体积筛，0 表示这一侧不限。
    subtitle 取 with（只看有字幕）/ without（只看缺字幕），空表示不筛。

    按番号排序能下推成 SQL，走的还是原来的分页快路径。按大小则不行 ——
    大小不存库，只能问磁盘 —— 这种情况下改走全表体积索引（同样带缓存），
    在内存里排完再切页。
    """
    page = max(1, page)
    size = min(max(1, size), 200)
    want_subtitle = subtitle if subtitle in ("with", "without") else ""
    # 按体积/字幕筛选或按体积排序时，结果依赖全表都已探过 ——
    # 没探过的行在 SQL 里是 NULL，会被静默排除掉
    need_probe = (
        min_gb > 0 or max_gb > 0 or bool(want_subtitle)
        or sort in ("size_desc", "size_asc")
    )

    with session_scope() as session:
        stmt = select(MediaLink)
        if keyword:
            like = f"%{keyword.strip()}%"
            stmt = stmt.where(or_(
                MediaLink.code.like(like),
                MediaLink.link_path.like(like),
                MediaLink.source_path.like(like),
                # 哈希 code 搜不出片名，得连别名表一起搜。source_path 里通常
                # 也含文件名，但直通模式改名后两者会不一致，别名才是准的
                MediaLink.code.in_(
                    select(CodeAlias.code).where(CodeAlias.filename.like(like))
                ),
            ))
        if missing_only:
            # 失效判定只能靠磁盘探测，没法下推成 SQL 条件；
            # 取到失效路径集合后当作 IN 条件用，分页仍然交给数据库
            stale = _missing_links()
            if not stale:
                return ResponseEntity.ok({
                    "items": [], "total": 0, "page": page, "size": size,
                    "delete_enabled": get_settings().medialink_delete_enabled,
                    "library_path": get_settings().medialink_library_path,
                    "sort": sort, "subtitle": want_subtitle,
                })
            stmt = stmt.where(MediaLink.link_path.in_(stale))

        # 筛选前先把没探过的补上。
        #
        # 这一步不能省：筛选是 SQL 条件，而存量记录的 size 是 NULL，
        # NULL >= 10GB 永远不成立 —— 筛出来是空的，而「就地补探当前页」
        # 又只作用于已经返回的行，于是空结果永远填不上自己。
        # 死锁在这儿：不先回填，这个筛选就永远返回空。
        #
        # 只在真的要筛（或按体积排序）时才做，且只补 NULL 的那些。
        # 一次探完整库在 NAS 上可能要等，但只会等这一次 —— 探过就落库了。
        if need_probe:
            pending = list(session.scalars(
                select(MediaLink).where(MediaLink.size_probe_time.is_(None))
                .limit(_FILTER_BACKFILL_CAP)
            ).all())
            if pending:
                logger.info(
                    f"体积筛选前回填 {len(pending)} 条未探测的关联"
                )
                _backfill_rows(session, pending)
                session.flush()

        # 体积筛选。大小已落库，直接下推成 SQL 条件。
        # 探不到大小的（size 为空）一律排除：留着会混在「大于 5G」里，
        # 而它究竟多大根本不知道
        if min_gb > 0:
            stmt = stmt.where(MediaLink.size >= int(min_gb * 1024 ** 3))
        if max_gb > 0:
            stmt = stmt.where(MediaLink.size <= int(max_gb * 1024 ** 3))

        # 字幕筛选。同样已落库。空值（没探过）不算有字幕，
        # 但也不该混进「缺字幕」—— 那是「不知道」，不是「没有」
        if want_subtitle == "with":
            stmt = stmt.where(MediaLink.has_subtitle.is_(True))
        elif want_subtitle == "without":
            stmt = stmt.where(MediaLink.has_subtitle.is_(False))

        total = session.scalar(
            select(func.count()).select_from(stmt.subquery())
        ) or 0

        # 排序全部下推成 SQL。次序键始终带上 link_path 兜底，否则同番号
        # 多条链接的相对次序在翻页间不稳定，会重复或漏行
        if sort == "code_asc":
            order = (MediaLink.code.asc(), MediaLink.link_path)
        elif sort == "code_desc":
            order = (MediaLink.code.desc(), MediaLink.link_path)
        elif sort == "time_asc":
            order = (MediaLink.create_time.asc(), MediaLink.link_path)
        elif sort == "size_desc":
            # 大小为空的排最后：不知道多大的不该占据「最大」的位置
            order = (
                MediaLink.size.is_(None), MediaLink.size.desc(),
                MediaLink.code, MediaLink.link_path,
            )
        elif sort == "size_asc":
            order = (
                MediaLink.size.is_(None), MediaLink.size.asc(),
                MediaLink.code, MediaLink.link_path,
            )
        else:
            order = (MediaLink.create_time.desc(), MediaLink.link_path)

        # 分页下推到 SQL：只有当前页的记录会被取出来做存在性探测
        rows = list(session.scalars(
            stmt.order_by(*order)
            .offset((page - 1) * size)
            .limit(size)
        ).all())

        # 大小与字幕已落库，正常情况下不再探盘。但存量记录这两列是空的，
        # 回填要等定时任务轮到 —— 让用户对着空白等半小时不合理。
        # 所以当前页里没有值的就地补探一次并写回库，只探这一页，
        # 下次翻回来就已经有值了
        stale = [r for r in rows if r.size_probe_time is None]
        if stale:
            _backfill_rows(session, stale)

        page_items = [{
            "link_path": r.link_path,
            "code": r.code,
            "source_path": r.source_path,
            "inode": r.inode,
            "device": r.device,
            "create_time": r.create_time.isoformat() if r.create_time else "",
            "link_exists": _exists_cached(r.link_path),
            "source_exists": _exists_cached(r.source_path),
            # null 表示还没探过，与「确认没有字幕」是两回事
            "has_subtitle": r.has_subtitle,
            # 字节数。null 表示还没探过或两侧文件都不在
            "size": r.size,
        } for r in rows]

    # 种子数与文件名别名都按番号批量查，避免每行一次查询
    codes = {i["code"] for i in page_items}
    counts: dict[str, int] = {}
    aliases: dict[str, str] = {}
    if codes:
        with session_scope() as session:
            counts = {
                code: n for code, n in session.execute(
                    select(History.code, func.count(History.hash))
                    .where(History.code.in_(codes))
                    .group_by(History.code)
                ).all()
            }
            # 长文件名的 code 是哈希，读不出是哪部片子，别名表里取回原名
            aliases = {
                code: name for code, name in session.execute(
                    select(CodeAlias.code, CodeAlias.filename)
                    .where(CodeAlias.code.in_(codes))
                ).all()
            }
    for item in page_items:
        item["torrent_count"] = counts.get(item["code"], 0)
        item["filename"] = aliases.get(item["code"], "")

    settings = get_settings()
    _attach_holds(page_items, settings.watchdir_delete_grace)
    return ResponseEntity.ok({
        "items": page_items,
        "total": total,
        "page": page,
        "size": size,
        # 页面要据此提示"联动删除未启用，删除只会演练"
        "delete_enabled": settings.medialink_delete_enabled,
        "library_path": settings.medialink_library_path,
        "sort": sort,
        "subtitle": want_subtitle,
    })


# 孤儿扫描的结果缓存。一轮要拉下载器全量种子清单 + 逐条 stat，
# 与 _missing_links 一个量级，同样得缓存，否则翻页每次都重扫
_ORPHAN_TTL = 300.0
_orphan_cache: tuple[float, list[dict]] | None = None


def _orphans_cached(force: bool = False) -> list[dict]:
    global _orphan_cache

    now = time.monotonic()
    if not force and _orphan_cache is not None and now - _orphan_cache[0] < _ORPHAN_TTL:
        return _orphan_cache[1]

    from app.services.orphan import scan_orphans

    items = scan_orphans()
    _orphan_cache = (now, items)
    return items


@router.get("/orphans")
def list_orphans(
    keyword: str = "",
    page: int = 1,
    size: int = 50,
    refresh: bool = False,
    current_user: str = Depends(get_current_user),
):
    """下载侧已删、媒体库侧仍在的关联。

    即「qb/tr 里删掉了，Emby 里还挂着」的那批 —— 点进去播不了的条目。
    只报告不删除，要动手请用现成的删除接口（联动删除或只删记录）。

    源文件消失与种子消失分开标注：只删种不删文件时源文件还占着空间，
    两者要做的事不一样，合并成一个状态会丢掉信息。
    """
    page = max(1, page)
    size = min(max(1, size), 200)

    items = _orphans_cached(force=refresh)

    if keyword:
        kw = keyword.strip().lower()
        items = [
            i for i in items
            if kw in i["code"].lower()
            or kw in i["link_path"].lower()
            or kw in i["source_path"].lower()
        ]

    # 删除时间倒序，最近删的排前面。源文件还在的（无删除时间）排最后 ——
    # 那些只是种子没了，不急着处理
    items = sorted(
        items,
        key=lambda i: (i["delete_time"] or "", i["create_time"] or ""),
        reverse=True,
    )

    total = len(items)
    start = (page - 1) * size
    return ResponseEntity.ok({
        "items": items[start:start + size],
        "total": total,
        "page": page,
        "size": size,
        "source_gone": sum(1 for i in items if i["source_gone"]),
        "torrent_gone": sum(1 for i in items if i["torrent_gone"]),
        "delete_enabled": get_settings().medialink_delete_enabled,
    })


@router.post("/recover")
def recover_medialinks(
    dry_run: bool = True, current_user: str = Depends(get_current_user)
):
    """从 History 反推，重建缺失的 media_link 记录。

    补的是「纳入管理」配不上的那批：那个按 inode 配对，要求源文件当前还在
    下载器里；这个按 History.save_path 配对，源文件已经删了也能重建。

    dry_run 默认为真 —— 重建的是反向删除的依据，配错了等于把删除权指向
    错误的源文件。先看清配对结果再落库。
    """
    from app.services.orphan import recover_records

    result = recover_records(dry_run=dry_run)
    if not dry_run:
        _drop_stats_cache()
    return ResponseEntity.ok(result)


@router.get("/stats")
def medialink_stats(
    check_missing: bool = True, current_user: str = Depends(get_current_user)
):
    """概览：关联总数、番号数、失效数、占用总量。

    总数、番号数与占用总量都是纯 SQL 聚合，不碰磁盘 —— 大小已落库。
    只有失效数仍要逐个探测文件是否还在，库大或挂在 NAS 上时明显更慢，
    走缓存；check_missing=false 可以跳过，此时 missing 返回 null。
    """
    with session_scope() as session:
        total = session.scalar(select(func.count()).select_from(MediaLink)) or 0
        codes = session.scalar(
            select(func.count(func.distinct(MediaLink.code)))
        ) or 0
        # 存量记录升级上来时这两列是空的，回填是渐进的，
        # 页面要能说清「总量还不完整」
        pending = session.scalar(
            select(func.count()).select_from(MediaLink)
            .where(MediaLink.size_probe_time.is_(None))
        ) or 0

    missing = len(_missing_links()) if check_missing else None
    # 纯 SQL，无需跟着 check_missing 一起跳过
    size = _total_size()
    return ResponseEntity.ok({
        "total": total,
        "codes": codes,
        "missing": missing,
        # 去重后的实际占用
        "size_bytes": size["bytes"],
        "size_files": size["files"],
        # 还没探过大小的记录数。回填没跑完时前端据此提示
        "size_pending": pending,
        "delete_enabled": get_settings().medialink_delete_enabled,
    })


@router.post("/register")
def register_medialink(
    body: RegisterRequest, current_user: str = Depends(get_current_user)
):
    """手工登记一条关联。webhook 没配好时的补救入口。"""
    code = (body.code or "").strip()
    source_path = (body.source_path or "").strip()
    if not code or not source_path:
        return ResponseEntity.fail("番号与源文件路径不能为空", code=400)

    links = register_scrape(code, source_path, (body.link_path or "").strip())
    if not links:
        return ResponseEntity.fail(
            "未找到可登记的硬链接。确认源文件存在、媒体库根目录已配置，"
            "且刮削产物与源文件在同一挂载卷",
            code=400,
        )
    _drop_stats_cache()
    return ResponseEntity.ok({"links": links}, message=f"已登记 {len(links)} 条")


@router.post("/subtitle")
def fetch_subtitle(
    body: SubtitleRequest, current_user: str = Depends(get_current_user)
):
    """给一个番号抓字幕。自动抓漏了、或想换一版时的手动入口。

    不看 SUBTITLE_ENABLED 开关 —— 那个管的是自动行为，人点了按钮就是
    明确要抓（与 qb 手动重启不看开关同理）。
    """
    code = (body.code or "").strip()
    if not code:
        return ResponseEntity.fail("番号不能为空", code=400)

    from app.services import subtitle as subtitle_service

    written = subtitle_service.fetch_for_code(code, force=body.force, manual=True)
    if not written:
        return ResponseEntity.fail(
            "未找到简体中文字幕。确认该番号已刮削入库，"
            "或稍后再试（字幕站收录有滞后）",
            code=404,
        )

    # 刚写完就把这个番号的探测结果踢掉，否则页面刷新后 TTL 未到，
    # 标记还是「无字幕」—— 用户会以为按钮没生效
    with session_scope() as session:
        paths = session.scalars(
            select(MediaLink.link_path).where(MediaLink.code == get_true_code(code))
        ).all()
    for path in paths:
        _subtitle_cache.pop(path, None)

    return ResponseEntity.ok(
        {"written": written}, message=f"已写入 {written} 处"
    )


@router.get("/review")
def get_review(code: str, current_user: str = Depends(get_current_user)):
    """读一个番号已生成的影评要点。没有则返回空。"""
    from app.database.models import Review

    code = get_true_code(code or "")
    if not code:
        return ResponseEntity.fail("番号不能为空", code=400)

    with session_scope() as session:
        row = session.get(Review, code)
        if row is None:
            return ResponseEntity.ok({})
        return ResponseEntity.ok({
            "code": row.code,
            "cast_count": row.cast_count,
            "body_type": row.body_type or "",
            "style": row.style or "",
            "highlights": (row.highlights or "").splitlines(),
            "summary": row.summary or "",
            "nfo_time": row.nfo_time.isoformat() if row.nfo_time else "",
            "update_time": row.update_time.isoformat() if row.update_time else "",
        })


@router.post("/review")
def generate_review(
    body: ReviewRequest, current_user: str = Depends(get_current_user)
):
    """给一个番号生成 AI 影评，并写进 NFO 与 Emby 简介。

    不看 REVIEW_ENABLED 开关 —— 那个管的是自动行为，人点了按钮就是明确
    要生成（与字幕手动重抓同理）。
    """
    code = (body.code or "").strip()
    if not code:
        return ResponseEntity.fail("番号不能为空", code=400)

    from app.services import review as review_service

    ok = review_service.generate_for_code(code, force=body.force, manual=True)
    if not ok:
        return ResponseEntity.fail(
            "未能生成影评。确认已配置 AI 接口（AI 助手或翻译任一），"
            "且该番号在库里有类别标签等元数据",
            code=400,
        )

    return ResponseEntity.ok({"code": get_true_code(code)}, message="已生成")


@router.post("/preview")
def preview_delete(
    body: DeleteRequest, current_user: str = Depends(get_current_user)
):
    """演练一次联动删除，只报会删什么，不动手。"""
    result = handle_media_deleted(
        link_path=body.link_path, code=body.code, dry_run=True
    )
    return ResponseEntity.ok(result.as_dict())


@router.post("/delete")
def delete_medialink(
    body: DeleteRequest, current_user: str = Depends(get_current_user)
):
    """执行联动删除：删种 → 删源文件 → 删硬链接 → 清记录。

    dry_run 默认为真，必须显式传 false 才会真删。全局开关
    MEDIALINK_DELETE_ENABLED 为假时，service 层还会再兜一次底。
    """
    if not body.link_path and not body.code:
        return ResponseEntity.fail("需要 link_path 或 code", code=400)

    result = handle_media_deleted(
        link_path=body.link_path, code=body.code, dry_run=body.dry_run
    )
    # 文件真被删掉了，缓存里的"存在"结论立刻作废
    _invalidate(*result.files_deleted, *result.links_deleted)
    _drop_stats_cache()
    if result.errors and not result.links_deleted:
        return ResponseEntity.fail("; ".join(result.errors), code=400, data=result.as_dict())
    return ResponseEntity.ok(result.as_dict())


@router.post("/batch-delete")
def batch_delete(
    body: BatchRequest, current_user: str = Depends(get_current_user)
):
    """批量联动删除。孤儿一览里勾中的那些一次删完。

    逐条走 handle_media_deleted，不合并成一次 —— 那个函数按 link_path 反查
    源文件与种子，每条的删除范围各不相同，合并没有意义。单条失败不中断
    整批：一条记录挂了（文件锁住、权限不足）不该让剩下的全都做不成，
    错误逐条收集，最后一并回报。

    dry_run 默认为真，与单条删除口径一致，必须显式传 false 才真删。
    """
    paths = [p for p in (body.link_paths or []) if p]
    if not paths:
        return ResponseEntity.fail("未选中任何记录", code=400)

    results: list[dict] = []
    errors: list[str] = []
    # 真删掉的条数。dry_run 时恒为 0，前端据此区分「演练」与「真删」
    deleted = 0
    downgraded = False

    # 整批选中记录的源文件，供批内一次性反查种子用 —— 否则每条都要重新
    # 拉一遍下载器的全量种子清单
    with session_scope() as session:
        sources = [
            r.source_path for r in session.scalars(
                select(MediaLink).where(MediaLink.link_path.in_(paths[:500]))
            ).all() if r.source_path
        ]

    # 批作用域：整批共用一次下载器查询。种子上千时这是分钟级与秒级的差别
    with torrent_batch() as batch:
        batch.preload_paths(sorted(set(sources)))
        for path in paths:
            try:
                outcome = handle_media_deleted(link_path=path, dry_run=body.dry_run)
            except Exception as exc:
                msg = f"{path}: {exc}"
                logger.error(f"批量联动删除异常 {msg}")
                errors.append(msg)
                continue

            # 全局开关关着时 service 层会把 dry_run 兜回真，据此提示用户
            if outcome.dry_run and not body.dry_run:
                downgraded = True
            elif not outcome.dry_run:
                deleted += 1

            _invalidate(*outcome.files_deleted, *outcome.links_deleted)
            errors.extend(f"{path}: {e}" for e in outcome.errors)
            results.append(outcome.as_dict())

    _drop_stats_cache()

    # 一条都没成功才算整体失败，部分成功照常返回，让前端展示明细
    if not results and errors:
        return ResponseEntity.fail("; ".join(errors[:5]), code=400)

    return ResponseEntity.ok({
        "total": len(paths),
        "deleted": deleted,
        "dry_run": downgraded or body.dry_run,
        "results": results,
        "errors": errors,
    })


@router.post("/batch-record")
def batch_drop_records(
    body: BatchRequest, current_user: str = Depends(get_current_user)
):
    """批量只删关联记录，不碰任何文件。

    孤儿一览的常见处置：文件早就手工删干净了，只想把库里这条没用的记录清掉。
    """
    paths = [p for p in (body.link_paths or []) if p]
    if not paths:
        return ResponseEntity.fail("未选中任何记录", code=400)

    removed: list[str] = []
    with session_scope() as session:
        # 分片：SQLite 的 IN 参数上限 999，一次全选很容易超
        for start in range(0, len(paths), 500):
            for row in session.scalars(
                select(MediaLink).where(
                    MediaLink.link_path.in_(paths[start:start + 500])
                )
            ).all():
                removed.append(row.link_path)
                session.delete(row)

    _drop_stats_cache()
    return ResponseEntity.ok({
        "removed": removed,
        # 选中却没删掉的（记录已被别处清掉），前端据此提示
        "missing": [p for p in paths if p not in set(removed)],
    })


@router.delete("/record")
def drop_record(
    link_path: str, current_user: str = Depends(get_current_user)
):
    """只删关联记录，不碰任何文件。用于清理失效的孤儿记录。"""
    if not link_path:
        return ResponseEntity.fail("缺少 link_path", code=400)

    with session_scope() as session:
        row = session.get(MediaLink, link_path)
        if row is None:
            return ResponseEntity.fail("记录不存在", code=404)
        session.delete(row)

    _drop_stats_cache()
    return ResponseEntity.ok(message="已删除记录")


@router.post("/refresh-sizes")
def refresh_link_sizes(
    limit: int = 500, force: bool = False,
    current_user: str = Depends(get_current_user),
):
    """回填文件大小与字幕状态。

    定时任务每半小时跑一批，这里是手动催一把的入口 —— 刚升级上来不想
    等回填慢慢跑完时用。force 连已探过的也重探，用于文件被换掉后校准。
    """
    from app.services.medialink import refresh_sizes

    result = refresh_sizes(limit=min(max(1, limit), 5000), force=force)
    _drop_stats_cache()
    return ResponseEntity.ok(
        result,
        message=(
            f"已探测 {result['probed']} 条，"
            f"剩余 {result['remaining']} 条未探"
        ),
    )


@router.post("/prune")
def prune_records(current_user: str = Depends(get_current_user)):
    """批量清掉文件已不存在的关联记录。同样不碰文件。

    这里要的是"两侧文件都没了"，比 _missing_links 的"任一侧没了"更严格，
    所以仍然自己扫一遍 —— 但探测走缓存，与刚打开页面时的那次共用结果。
    """
    with session_scope() as session:
        rows = session.execute(
            select(MediaLink.link_path, MediaLink.source_path)
        ).all()
        stale = [
            link for link, source in rows
            if not _exists_cached(link) and not _exists_cached(source)
        ]
        for path in stale:
            row = session.get(MediaLink, path)
            if row is not None:
                session.delete(row)

    _drop_stats_cache()
    return ResponseEntity.ok(
        {"removed": stale}, message=f"已清理 {len(stale)} 条失效记录"
    )
