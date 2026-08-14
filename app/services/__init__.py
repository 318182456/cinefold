"""业务服务层。

编排各模块完成完整业务链路：搜种 → 过滤 → 下载 → 记录 → 通知。
本层不直接处理 HTTP，只依赖 modules 与 database。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, or_, select

from app.core.config import get_settings
from app.database.models import Actor, Code, CodeStatus, History
from app.database.session import session_scope
from app.modules import downloadclient, mediaserver, notify, ptsite, translate
from app.schemas.torrent import Torrent
from app.utils import get_magnet_hash
from app.utils.filters import filter_torrents, has_vr, sort_torrents


# ======================================================================
# 搜索
# ======================================================================
# 全站检索一个番号要十几秒，短时间内重复搜同一个番号的场景很常见
# （消息重投、手动重试、订阅任务与消息订阅撞车）。缓存原始结果，
# 过滤排序仍每次现算，改了过滤条件不必等缓存过期。
TORRENT_CACHE_TTL = 1800

# 与 app.modules.bt.bt.BT.name 一致，用于按站点过滤
BT_SITE_NAME = "BT"


def search_torrents(
    code: str, use_filter: bool = True, refresh: bool = False
) -> list[Torrent]:
    """搜索番号对应的种子，按配置过滤排序后返回。

    refresh=True 时跳过缓存强制重搜。
    """
    settings = get_settings()
    torrents = _search_pt_cached(code, refresh=refresh)
    if not torrents:
        return []

    for torrent in torrents:
        torrent.code = torrent.code or code

    if use_filter:
        torrents = filter_torrents(torrents, settings.default_filter)

    return sort_torrents(torrents, settings.default_sort, build_site_priority())


def _search_pt_cached(code: str, refresh: bool = False) -> list[Torrent]:
    """带缓存的全站检索。缓存的是各站返回的原始种子列表。"""
    import json

    key = code.upper()
    if not refresh:
        cached = get_rank_cache("torrent", key, ttl=TORRENT_CACHE_TTL)
        if cached:
            try:
                items = json.loads(cached)
                logger.info(f"[{code}] 命中检索缓存，{len(items)} 个种子")
                return [Torrent.from_dict(item) for item in items]
            except (ValueError, TypeError):
                logger.debug(f"[{code}] 检索缓存解析失败，重新搜索")

    torrents, ok_sites, total_sites = ptsite.search_pt_detailed(code)

    # 空结果也缓存：搜不到的番号往往一段时间内都搜不到，
    # 否则每次重投消息都要再跑一轮全站检索。
    #
    # 但"没问成"不等于"没有"——站点没配、鉴权失败、超时都会返回空列表，
    # 把它当结论缓存下来，接下来 TTL 内的每次搜索都会直接返回空，
    # 连补好配置也救不回来，只能干等过期
    if torrents or ok_sites:
        set_rank_cache(
            "torrent", key,
            json.dumps([t.to_dict() for t in torrents], ensure_ascii=False),
        )
    else:
        logger.warning(
            f"[{code}] {total_sites} 个站点全部搜索失败或未配置，空结果不缓存"
        )
    return torrents


def build_site_priority() -> list[str]:
    """站点优先级：主站在前，其余按注册顺序补齐。

    主站可能配的是已停用的站，保留在列表里无副作用——排序时不会有它的种子。
    """
    settings = get_settings()
    names = [site.name for site in ptsite.get_sites()]
    lookup = {name.lower(): name for name in names}

    primary: list[str] = []
    for raw in (settings.primary_site or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        # 用站点自身的大小写，与 Torrent.site 保持一致
        name = lookup.get(raw.lower(), raw)
        if name not in primary:
            primary.append(name)

    return primary + [name for name in names if name not in primary]


def find_torrent(code: str) -> Torrent | None:
    """取排序后最优的一个种子。自动下载专用。

    关掉 BT_AUTO_DOWNLOAD 时跳过 BT 源——它仍参与搜索、结果在页面上
    可见可手动下载，只是不进自动选种。
    """
    results = search_torrents(code)
    if not get_settings().bt_auto_download:
        results = [t for t in results if t.site != BT_SITE_NAME]
    return results[0] if results else None


def search_code(keyword: str, limit: int = 50) -> list[dict]:
    """在本地库中按番号或标题搜索。

    用户常输入不带横杠的写法（jul915），库里存的是标准形式（JUL-915），
    直接 LIKE 匹配不上会白跑一趟远程抓取，所以先按番号规则归一化再查。
    """
    from app.utils import get_true_code

    keyword = (keyword or "").strip()
    if not keyword:
        return []

    conditions = []
    pattern = f"%{keyword}%"
    conditions.append(Code.code.like(pattern))
    conditions.append(Code.title.like(pattern))
    conditions.append(Code.cn_title.like(pattern))

    normalized = get_true_code(keyword)
    if normalized and normalized.upper() != keyword.upper():
        conditions.append(Code.code.like(f"%{normalized}%"))

    with session_scope() as session:
        rows = session.scalars(
            select(Code)
            .where(or_(*conditions))
            .order_by(Code.release_date.desc())
            .limit(limit)
        ).all()
        return [row.to_dict() for row in rows]


def cache_remote_codes(items: list[dict]) -> int:
    """把远程搜到的番号情报落库，下次搜同一个番号直接走本地。

    只补空字段，不覆盖已有值，更不动 status——库里可能已经订阅或下载过了。
    """
    if not items:
        return 0

    # 番号之外的字段才值得写库，status 之类的状态字段绝不能被远程数据带跑
    skip = {"code", "status", "create_time", "update_time"}
    saved = 0

    with session_scope() as session:
        for item in items:
            code = (item.get("code") or "").strip().upper()
            if not code:
                continue

            row = session.get(Code, code)
            if row is None:
                row = Code(code=code)
                session.add(row)

            for key, value in item.items():
                if key in skip or not value:
                    continue
                if hasattr(row, key) and not getattr(row, key):
                    setattr(row, key, value)
            saved += 1

    if saved:
        logger.info(f"已缓存 {saved} 个番号情报")
    return saved


def search_actor(keyword: str, limit: int = 50) -> list[dict]:
    with session_scope() as session:
        pattern = f"%{keyword}%"
        rows = session.scalars(
            select(Actor)
            .where(Actor.name.like(pattern) | Actor.name_2.like(pattern))
            .limit(limit)
        ).all()
        return [row.to_dict() for row in rows]


# ======================================================================
# 媒体库
# ======================================================================
# 媒体库内容分钟级不变，同一个番号在一轮消息处理里会被查两次
# （先 _split_existing 筛一遍，download_torrent 里再查一次）
MEDIA_EXISTS_CACHE_TTL = 600


def is_exist_server(code: str) -> bool:
    """番号是否已在任一媒体库中。带短时缓存，避免同一轮内重复查询。"""
    if not code:
        return False

    key = code.upper()
    cached = get_rank_cache("media", key, ttl=MEDIA_EXISTS_CACHE_TTL)
    if cached is not None:
        return cached == "1"

    exists = mediaserver.exists_in_any(code)
    set_rank_cache("media", key, "1" if exists else "0")
    return exists


# ======================================================================
# 下载
# ======================================================================
def download_torrent(code: str, torrent: Torrent | None = None) -> bool:
    """下载指定番号。torrent 为空时自动搜索最优种子。"""
    settings = get_settings()

    # 种子名未必带 VR 标记，靠 filter_torrents 拦不住，这里用番号情报再判一次
    reason = is_filtered_code(code)
    if reason:
        logger.info(f"[{code}] 命中 {reason} 过滤，不下载")
        return False

    # 已入库的直接跳过，避免重复占用带宽
    if settings.enable_auto_complete and is_exist_server(code):
        logger.info(f"[{code}] 媒体库中已存在，跳过下载")
        _update_code_status(code, CodeStatus.COMPLETED)
        return False

    # 查历史比搜种便宜得多，放在搜索前面短路，省掉一轮全站检索
    if _is_downloaded(code):
        # 已推过下载器却仍是待下载，说明状态没跟上，补正一次；
        # 否则每轮订阅任务都会重新搜一遍，番号永远留在订阅列表里
        logger.info(f"[{code}] 已下载过，跳过")
        _sync_status_from_history(code)
        return False

    torrent = torrent or find_torrent(code)
    if torrent is None:
        logger.info(f"[{code}] 未搜到符合条件的种子")
        return False

    client = downloadclient.get_download_client()
    if client is None:
        logger.error(f"[{code}] 未配置下载器")
        return False

    torrent_hash = _push_to_client(client, torrent, code)
    if not torrent_hash:
        _update_code_status(code, CodeStatus.FAILED)
        return False

    _record_history(code, torrent_hash)
    _update_code_status(code, CodeStatus.DOWNLOADING)
    send_downloading_message(code, torrent)
    return True


def _push_to_client(client, torrent: Torrent, code: str) -> str | None:
    """磁链直接推送，http 种子先下载再推送。"""
    url = torrent.download_url

    if url.startswith("magnet:"):
        return client.add_torrent_by_magnet(url, code)

    site = ptsite.get_site_by_name(torrent.site)
    content = site.download_seed(torrent) if site else None
    if content:
        return client.add_torrent(content, code)

    # 拿不到种子文件时，若 url 本身是磁链之外的直链也试一次
    if url:
        return client.add_torrent_by_magnet(url, code)

    logger.warning(f"[{code}] 无法获取种子内容")
    return None


def _is_downloaded(code: str) -> bool:
    with session_scope() as session:
        return session.scalar(
            select(func.count()).select_from(History).where(History.code == code)
        ) > 0


def _sync_status_from_history(code: str) -> None:
    """有下载记录却仍标记为待下载时，把状态补正为已推送。

    sync_download_status 只扫 DOWNLOADING 的番号，卡在 SUBSCRIBED 的
    永远等不到它接手，只能每轮重新搜索。这里补上这一跳。
    """
    with session_scope() as session:
        row = session.get(Code, code)
        if row is not None and row.status == CodeStatus.SUBSCRIBED:
            row.status = CodeStatus.DOWNLOADING
            row.update_time = datetime.now()
            logger.info(f"[{code}] 有下载记录，状态补正为下载中")


def _record_history(code: str, torrent_hash: str, save_path: str = "") -> None:
    with session_scope() as session:
        if session.get(History, torrent_hash) is None:
            session.add(History(hash=torrent_hash, code=code, save_path=save_path))


def _update_code_status(code: str, status: int) -> None:
    with session_scope() as session:
        row = session.get(Code, code)
        if row is not None:
            row.status = status
            row.update_time = datetime.now()


# ======================================================================
# 订阅任务
# ======================================================================
def run_sub_task() -> int:
    """跑一轮订阅下载：把所有已订阅但未下载的番号推给下载器。"""
    with session_scope() as session:
        codes = session.scalars(
            select(Code.code).where(Code.status == CodeStatus.SUBSCRIBED)
        ).all()

    if not codes:
        logger.info("没有待下载的订阅")
        return 0

    logger.info(f"开始处理 {len(codes)} 个订阅")
    success = sum(1 for code in codes if download_torrent(code))
    logger.info(f"订阅任务完成，成功 {success}/{len(codes)}")
    return success


def download_codes_async(codes: list[str], notify_result: bool = True) -> None:
    """后台检索并下载指定番号。

    给消息订阅用：webhook 里同步搜种会卡住请求，Telegram 超时后会重投同一条
    消息，导致重复订阅，所以丢到后台线程。
    """
    if not codes:
        return

    from app.utils import run_in_background

    def _task() -> None:
        started, missed = [], []
        for code in codes:
            try:
                (started if download_torrent(code) else missed).append(code)
            except Exception as exc:
                logger.warning(f"[{code}] 自动检索失败: {exc}")
                missed.append(code)

        logger.info(f"消息订阅自动检索完成，已推送 {len(started)}/{len(codes)}")
        if not notify_result or not missed:
            return
        # download_torrent 成功时自身会推送通知，这里只补一条未命中的汇总
        send_message(f"🔎 自动检索：{len(missed)} 个暂无资源\n{', '.join(missed)}")

    run_in_background(_task)


# 演员没设起始日期时，只回溯这么多天，避免把全部历史作品拉进订阅
ACTOR_FALLBACK_DAYS = 30
# 单个演员单轮最多新增多少订阅，防止一次刷爆订阅列表
ACTOR_SUBSCRIBE_LIMIT = 50


def run_run_actor() -> int:
    """演员订阅：把已订阅演员的新作品加入订阅队列。"""
    with session_scope() as session:
        actors = session.scalars(select(Actor)).all()
        actor_names = [(a.name, a.limit_date) for a in actors]

    if not actor_names:
        return 0

    total = 0
    for name, limit_date in actor_names:
        total += _subscribe_actor_new_works(name, limit_date)

    logger.info(f"演员订阅完成，新增 {total} 个番号")
    return total


def _subscribe_actor_new_works(actor_name: str, limit_date: str | None) -> int:
    """把某演员在 limit_date 之后的作品标记为已订阅。

    limit_date 为空时只看最近 ACTOR_FALLBACK_DAYS 天的新作。老数据里
    这一列可能没值，不设兜底就会把该演员的全部历史作品一次性订阅掉。
    """
    if not limit_date:
        limit_date = (
            datetime.now() - timedelta(days=ACTOR_FALLBACK_DAYS)
        ).strftime("%Y-%m-%d")
        logger.debug(f"[{actor_name}] 未设起始日期，只订阅 {limit_date} 之后的新作")

    with session_scope() as session:
        query = select(Code).where(
            Code.casts.like(f"%{actor_name}%"),
            Code.status == CodeStatus.NONE,
            Code.release_date >= limit_date,
        )

        rows = session.scalars(query.limit(ACTOR_SUBSCRIBE_LIMIT)).all()

        # 这里是批量改状态，不走 subscribe_code，过滤要自己判一次
        exclude_vr = bool((get_settings().default_filter or {}).get("exclude_vr"))
        wanted, skipped = [], 0
        for row in rows:
            if exclude_vr and has_vr(f"{row.code} {row.title or ''}"):
                skipped += 1
                continue
            wanted.append(row)

        for row in wanted:
            row.status = CodeStatus.SUBSCRIBED

        if wanted:
            logger.info(f"[{actor_name}] 新增订阅 {len(wanted)} 个番号")
        if skipped:
            logger.info(f"[{actor_name}] 跳过 {skipped} 个 VR 番号")
        return len(wanted)


def is_filtered_code(code: str, title: str = "") -> str:
    """番号本身是否该被过滤掉。返回原因，空串表示放行。

    种子层的过滤看的是种子名，而 PT 站的种子名未必带 VR 标记，
    等到那时才拦已经晚了——番号早就进了订阅列表。这里按番号情报
    的标题先筛一道。
    """
    config = get_settings().default_filter or {}
    if not config.get("exclude_vr"):
        return ""

    if not title:
        with session_scope() as session:
            row = session.get(Code, code)
            title = (row.title or row.cn_title or "") if row else ""

    # 番号本身也过一遍，DSVR-123 这类前缀能直接看出来
    if has_vr(f"{code} {title}"):
        return "VR"
    return ""


def subscribe_code(code: str) -> bool:
    """订阅单个番号。被过滤规则拦下时返回 False。"""
    reason = is_filtered_code(code)
    if reason:
        logger.info(f"[{code}] 命中 {reason} 过滤，不订阅")
        return False

    with session_scope() as session:
        row = session.get(Code, code)
        if row is None:
            row = Code(code=code, status=CodeStatus.SUBSCRIBED)
            session.add(row)
        else:
            row.status = CodeStatus.SUBSCRIBED
    send_subscribe_message(code)
    return True


def cancel_subscribe(code: str) -> bool:
    with session_scope() as session:
        row = session.get(Code, code)
        if row is None:
            return False
        row.status = CodeStatus.NONE
    return True


def cancel_subscribe_codes(codes: list[str]) -> dict:
    """按番号列表批量取消订阅。返回真正改动的番号与未命中的。

    列表页多选用这个入口，一次事务提交，比前端循环打 N 个请求快得多。
    """
    wanted = [c for c in dict.fromkeys(codes) if c]
    if not wanted:
        return {"cancelled": [], "missing": []}

    with session_scope() as session:
        rows = session.scalars(select(Code).where(Code.code.in_(wanted))).all()
        found = {row.code for row in rows}
        for row in rows:
            row.status = CodeStatus.NONE

    missing = [c for c in wanted if c not in found]
    logger.info(f"批量取消订阅 {len(found)} 个番号，{len(missing)} 个不存在")
    return {"cancelled": sorted(found), "missing": missing}


def subscribe_codes(codes: list[str]) -> dict:
    """按番号列表批量订阅。被过滤规则拦下的单独列出。"""
    wanted = [c for c in dict.fromkeys(codes) if c]
    subscribed: list[str] = []
    filtered: list[str] = []
    for code in wanted:
        if subscribe_code(code):
            subscribed.append(code)
        else:
            filtered.append(code)
    return {"subscribed": subscribed, "filtered": filtered}


def bulk_cancel_subscribe(
    before_date: str = "",
    only_vr: bool = False,
    keep_recent_days: int = 0,
    dry_run: bool = True,
) -> dict:
    """批量取消订阅。默认只试算，dry_run=False 才真正执行。

    演员订阅曾经把全部历史作品一次性拉进来，需要一个批量清理的入口。
    只动"已订阅"的，下载中/已下载/已入库的不碰。
    """
    conditions = [Code.status == CodeStatus.SUBSCRIBED]

    if before_date:
        conditions.append(Code.release_date < before_date)
    elif keep_recent_days > 0:
        cutoff = (
            datetime.now() - timedelta(days=keep_recent_days)
        ).strftime("%Y-%m-%d")
        conditions.append(Code.release_date < cutoff)

    with session_scope() as session:
        rows = session.scalars(select(Code).where(*conditions)).all()

        if only_vr:
            rows = [r for r in rows if has_vr(f"{r.code} {r.title or ''}")]

        samples = [
            {"code": r.code, "title": (r.title or "")[:40],
             "release_date": r.release_date}
            for r in rows[:20]
        ]
        matched = len(rows)

        if not dry_run:
            for row in rows:
                row.status = CodeStatus.NONE
            logger.info(f"批量取消订阅 {matched} 个番号")

    return {
        "matched": matched,
        "cancelled": 0 if dry_run else matched,
        "dry_run": dry_run,
        "samples": samples,
    }


def subscribe_actor(name: str, limit_date: str = "") -> bool:
    """订阅演员。不指定起始日期时从今天算起。

    留空等于把该演员的全部历史作品都订阅了——库里番号上万时
    一个热门演员就能刷出几百条，几乎不会是用户想要的。
    """
    default_date = datetime.now().strftime("%Y-%m-%d")
    with session_scope() as session:
        row = session.get(Actor, name)
        if row is None:
            session.add(Actor(name=name, limit_date=limit_date or default_date))
        else:
            row.limit_date = limit_date or row.limit_date or default_date
    send_subscribe_actor_message(name)
    return True


def cancel_actor(name: str) -> bool:
    with session_scope() as session:
        row = session.get(Actor, name)
        if row is None:
            return False
        session.delete(row)
    return True


def purge_migrated_actors(dry_run: bool = True) -> dict:
    """清掉迁移带进来的伪订阅演员。

    老版本的 actor 表是演员资料缓存，迁移时整表搬了过来，在新版语义下
    全都成了"已订阅"，演员订阅任务每轮都会拿它们去刷番号。
    真实订阅一定带 limit_date（subscribe_actor 为空时会填当天），
    没有这一列值的就是迁移残留。
    """
    with session_scope() as session:
        condition = (Actor.limit_date.is_(None)) | (Actor.limit_date == "")
        rows = session.scalars(select(Actor).where(condition)).all()
        matched = len(rows)
        samples = [row.name for row in rows[:20]]
        kept = session.scalar(
            select(func.count()).select_from(Actor).where(~condition)
        ) or 0

        if not dry_run:
            for row in rows:
                session.delete(row)
            logger.info(f"清理迁移残留演员 {matched} 条，保留真实订阅 {kept} 条")

    return {
        "matched": matched,
        "deleted": 0 if dry_run else matched,
        "kept": kept,
        "dry_run": dry_run,
        "samples": samples,
    }


# ======================================================================
# 下载状态同步
# ======================================================================
def sync_download_status() -> int:
    """查询下载器，把已完成的任务更新为下载完成并通知。

    刚下完的任务顺手转移做种（若已开启），不必等下一轮定时扫描 —— 这里
    已经知道是哪几个刚完成，比全量拉一次 qb 列表省事得多。
    """
    client = downloadclient.get_download_client()
    if client is None:
        return 0

    with session_scope() as session:
        pairs = session.execute(
            select(History.hash, History.code)
            .join(Code, Code.code == History.code)
            .where(Code.status == CodeStatus.DOWNLOADING)
        ).all()

    if not pairs:
        return 0

    hash_to_code = dict(pairs)
    states = client.monitor_torrent(list(hash_to_code.keys()))

    completed = 0
    just_done: list[str] = []
    for state in states:
        if not state.get("completed"):
            continue
        torrent_hash = state.get("hash", "")
        code = hash_to_code.get(torrent_hash)
        if not code:
            continue
        _update_code_status(code, CodeStatus.DOWNLOADED)
        _update_save_path(torrent_hash, state.get("save_path", ""))
        send_downloaded_message(code)
        just_done.append(torrent_hash)
        completed += 1

    if completed:
        logger.info(f"{completed} 个任务下载完成")
        _transfer_just_completed(client, just_done)
    return completed


def _transfer_just_completed(client, hashes: list[str]) -> None:
    """把刚下完的任务转移到 tr 做种。未开启或条件不满足时静默跳过。

    只在默认下载器是 qb 时做 —— 这批 hash 来自默认下载器，默认是 tr 时
    它们本就在 tr 里，拿去 transfer_hashes 只会得到一串「qb 中找不到」。

    转移失败不影响下载完成本身，异常一律吞掉：这是搭在同步流程上的顺带
    动作，不能让它把状态更新和通知带崩。漏掉的下一轮定时扫描会补上。
    """
    if not hashes:
        return

    settings = get_settings()
    if not settings.seed_transfer_enabled:
        return

    from app.modules.downloadclient.qbittorrent import QBitTorrentClient
    if not isinstance(client, QBitTorrentClient):
        logger.debug("[转移做种] 默认下载器不是 qBittorrent，跳过即时转移")
        return

    try:
        from app.services import seedtransfer

        ok, reason = seedtransfer.is_available()
        if not ok:
            logger.debug(f"[转移做种] {reason}，跳过即时转移")
            return

        # 同样守单轮上限：一批下完好几十个时，这里不设限就绕开了配置，
        # tr 的校验会把磁盘打满。超出的留给定时任务下一轮
        limit = seedtransfer._batch_limit()
        if len(hashes) > limit:
            logger.info(
                f"[转移做种] 刚完成 {len(hashes)} 个，超出单轮上限 {limit}，"
                f"本次转移前 {limit} 个，其余等定时任务"
            )
            hashes = hashes[:limit]

        result = seedtransfer.transfer_hashes(hashes)
        if result.count:
            logger.info(f"[转移做种] 下载完成后即时转移 {result.count} 个")
    except Exception as exc:
        logger.warning(f"[转移做种] 下载完成后即时转移失败: {exc}")


def _update_save_path(torrent_hash: str, save_path: str) -> None:
    if not save_path:
        return
    with session_scope() as session:
        row = session.get(History, torrent_hash)
        if row is not None:
            row.save_path = save_path


# ======================================================================
# 翻译
# ======================================================================
# 并发翻译的线程数。翻译接口按请求计费/限流，不宜开太大
TRANSLATE_WORKERS = 4
# 连续这么多条翻译不出来就认定服务不可用，本轮不再继续
TRANSLATE_FAILURE_LIMIT = 5


def translate_title(title: str) -> str:
    return translate.translate(title)


def translate_codes(limit: int = 50) -> int:
    """批量翻译缺中文标题的番号。"""
    if not translate.is_available():
        return 0

    with session_scope() as session:
        rows = session.scalars(
            select(Code)
            .where(Code.title.isnot(None), Code.title != "")
            .where((Code.cn_title.is_(None)) | (Code.cn_title == ""))
            .limit(limit)
        ).all()
        pending = [(row.code, row.title) for row in rows]

    if not pending:
        return 0

    from concurrent.futures import ThreadPoolExecutor
    from itertools import count as _count

    # 翻译服务挂掉时每条都要撞一次超时，连续失败到阈值就本轮收工
    failures = _count()
    give_up = threading.Event()

    def run(item: tuple[str, str]) -> tuple[str, str]:
        code, title = item
        if give_up.is_set():
            return code, ""
        try:
            translated = translate_title(title)
        except Exception as exc:
            logger.debug(f"[{code}] 翻译失败: {exc}")
            translated = ""

        if translated:
            return code, translated
        if next(failures) >= TRANSLATE_FAILURE_LIMIT:
            give_up.set()
        return code, ""

    # 翻译接口单次 1~5 秒，串行 50 条要好几分钟
    workers = min(TRANSLATE_WORKERS, len(pending))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = [(c, t) for c, t in pool.map(run, pending) if t]

    if give_up.is_set():
        logger.warning("翻译连续失败，本轮提前结束，请检查翻译服务是否可用")

    if not results:
        return 0

    count = 0
    with session_scope() as session:
        for code, translated in results:
            row = session.get(Code, code)
            if row is not None:
                row.cn_title = translated
                count += 1

    if count:
        logger.info(f"已翻译 {count} 个标题")
    return count


# ======================================================================
# 数据补全
# ======================================================================
def fill_lack_codes(limit: int = 50) -> int:
    """补全缺详情的番号。需要资源站模块，未接入时静默跳过。"""
    try:
        from app.modules import ladysite
    except ImportError:
        return 0

    with session_scope() as session:
        codes = session.scalars(
            select(Code.code)
            .where((Code.title.is_(None)) | (Code.title == ""))
            .limit(limit)
        ).all()

    return fill_lack_codes_by_list(list(codes))


def fill_lack_codes_by_list(codes: list[str]) -> int:
    """并发抓取番号详情并批量入库。

    每个番号要跨境抓 1~4 次（多站 fallback），串行 50 个能跑好几分钟。
    抓取放线程池，写库合并成一个事务。
    """
    if not codes:
        return 0
    try:
        from app.modules import ladysite
    except ImportError:
        logger.debug("资源站模块未接入，跳过补全")
        return 0

    from concurrent.futures import ThreadPoolExecutor

    def fetch(code: str) -> tuple[str, dict]:
        try:
            return code, ladysite.get_code_detail(code)
        except Exception as exc:
            logger.debug(f"[{code}] 抓取详情失败: {exc}")
            return code, {}

    workers = min(DETAIL_FETCH_WORKERS, len(codes))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = [(c, d) for c, d in pool.map(fetch, codes) if d]

    if not results:
        return 0

    count = 0
    with session_scope() as session:
        for code, detail in results:
            row = session.get(Code, code)
            if row is None:
                continue
            for key, value in detail.items():
                if value and hasattr(row, key):
                    setattr(row, key, value)
            count += 1

    if count:
        logger.info(f"已补全 {count} 个番号详情")
    return count


# 一轮最多拉多少张封面。图源在墙外，量太大会让任务跑很久
PHOTO_CACHE_LIMIT = 100
# 并发数。图源对单 IP 的连接数敏感，开太大反而容易被限速
PHOTO_CACHE_WORKERS = 5


def cache_lack_photos(limit: int = PHOTO_CACHE_LIMIT) -> int:
    """把还没落盘的封面批量拉到本地。

    列表页每张卡都要一张封面，没缓存过的要现场回源，翻一页就得等一批。
    提前拉下来后 image-local 直接读盘，页面立刻出图。
    """
    import httpx
    from concurrent.futures import ThreadPoolExecutor

    from app.utils import imagecache

    settings = get_settings()

    with session_scope() as session:
        rows = session.execute(
            select(Code.code, Code.banner, Code.poster)
            .where((Code.local_banner.is_(None)) | (Code.local_banner == ""))
            .where((Code.banner.isnot(None)) & (Code.banner != ""))
            .limit(limit)
        ).all()

    pending = [(code, banner or poster) for code, banner, poster in rows]
    pending = [(code, url) for code, url in pending if url]
    if not pending:
        return 0

    def fetch(item: tuple[str, str]) -> tuple[str, str] | None:
        code, url = item
        # 已经在盘上但库里没记，直接回填省一次下载
        hit = imagecache.find_cached(url, code, "banner")
        if hit is not None:
            return code, imagecache.relative_of(hit)

        try:
            with httpx.Client(
                timeout=httpx.Timeout(20.0, connect=10.0),
                follow_redirects=True,
                proxy=settings.proxy or None,
                headers={
                    "Referer": "https://www.javbus.com/",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                    ),
                },
            ) as client:
                response = client.get(url)
                response.raise_for_status()
                content = response.content
        except Exception as exc:
            logger.debug(f"[{code}] 封面下载失败: {exc}")
            return None

        stored = imagecache.store(content, url, code, "banner")
        if stored is None:
            return None
        return code, imagecache.relative_of(stored)

    with ThreadPoolExecutor(max_workers=PHOTO_CACHE_WORKERS) as pool:
        results = [r for r in pool.map(fetch, pending) if r and r[1]]

    if not results:
        return 0

    with session_scope() as session:
        for code, relative in results:
            row = session.get(Code, code)
            if row is not None:
                row.local_banner = relative

    logger.info(f"已缓存 {len(results)} 张封面")
    return len(results)


def fill_lack_actors(limit: int = 50) -> int:
    with session_scope() as session:
        names = session.scalars(
            select(Actor.name)
            .where((Actor.photo.is_(None)) | (Actor.photo == ""))
            .limit(limit)
        ).all()
    return fill_lack_actors_by_list(list(names))


def fill_lack_actors_by_list(names: list[str]) -> int:
    """并发抓演员头像并批量入库。串行 50 个要跑一两分钟。"""
    if not names:
        return 0
    try:
        from app.modules import ladysite
    except ImportError:
        return 0

    from concurrent.futures import ThreadPoolExecutor

    def fetch(name: str) -> tuple[str, str]:
        try:
            return name, ladysite.get_actor_photo(name)
        except Exception as exc:
            logger.debug(f"[{name}] 抓取头像失败: {exc}")
            return name, ""

    workers = min(DETAIL_FETCH_WORKERS, len(names))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = [(n, p) for n, p in pool.map(fetch, names) if p]

    if not results:
        return 0

    count = 0
    with session_scope() as session:
        for name, photo in results:
            row = session.get(Actor, name)
            if row is not None:
                row.photo = photo
                count += 1
    return count


# ======================================================================
# 榜单与厂牌
# ======================================================================
# 榜单页每次现抓要等十几秒，缓存一段时间；日榜一天只变一次，半小时足够新鲜
RANK_CACHE_TTL = 1800
BRAND_CACHE_TTL = 3600

# 单次请求最多现抓多少条详情。剩下的留给「补全缺图」定时任务慢慢补，
# 否则打开榜单页要等几十次串行抓取。
DETAIL_FETCH_LIMIT = 8

# 并发抓详情的线程数。同 host 仍受 SiteClient 节流排队，
# 这里的收益主要来自多站 fallback 时不同域名可以并行
DETAIL_FETCH_WORKERS = 4


def _rows_by_code(codes: list[str]) -> dict[str, dict]:
    """批量取库里已有的番号详情。"""
    if not codes:
        return {}
    with session_scope() as session:
        rows = session.scalars(select(Code).where(Code.code.in_(codes))).all()
        return {row.code: row.to_dict() for row in rows}


def _has_detail(item: dict) -> bool:
    return bool(item.get("title")) and bool(item.get("banner") or item.get("poster"))


def enrich_codes(codes: list[str], fetch_limit: int | None = None) -> list[dict]:
    """把番号列表补成带标题封面的完整条目。

    先查本地库，缺的现抓一小批并入库，其余留空由定时任务补。
    榜单接口只拿到番号，不补的话前端只能显示一排光秃秃的番号。

    fetch_limit 传 None 时读模块级默认值，这样运行时调整能立即生效。
    """
    if not codes:
        return []

    limit = DETAIL_FETCH_LIMIT if fetch_limit is None else fetch_limit

    known = _rows_by_code(codes)
    missing = [c for c in codes if not _has_detail(known.get(c, {}))]

    if missing:
        # 先确保这些番号在库里，后续补全和订阅都依赖行存在
        _ensure_codes(missing)

        # 每个番号要跨境抓 1~4 次，串行下来榜单页首次打开要等几十秒。
        # 这是用户直接等待的路径，并发抓完再统一写库
        if fill_lack_codes_by_list(missing[:limit]):
            known = _rows_by_code(codes)

    out = []
    for code in codes:
        out.append(known.get(code) or {"code": code, "status": CodeStatus.NONE})
    return out


def _ensure_codes(codes: list[str]) -> None:
    """番号占位入库，已存在则跳过。"""
    from app.database.session import batch_insert_ignore_duplicate

    with session_scope() as session:
        batch_insert_ignore_duplicate(
            session, Code, [{"code": c} for c in codes]
        )


def get_rank_items(rank_type: str = "") -> list[dict]:
    """榜单，带详情与缓存。"""
    import json

    key = (rank_type or "daily").strip().lower()
    cached = get_rank_cache("rank", key, ttl=RANK_CACHE_TTL)
    if cached:
        try:
            return json.loads(cached)
        except ValueError:
            logger.debug("榜单缓存解析失败，重新抓取")

    try:
        from app.modules import ladysite
        codes = [item["code"] for item in ladysite.get_rank(rank_type) if item.get("code")]
    except (ImportError, AttributeError):
        return []

    items = enrich_codes(codes)
    if items:
        set_rank_cache("rank", key, json.dumps(items, ensure_ascii=False, default=str))
    return items


def get_brand_items(brand: str, past_days: int = 7, future_days: int = 14) -> list[dict]:
    """某个厂牌的新片与预定发布，带详情与缓存。

    future_days 覆盖官网已挂出的预定发布日期，这是旧版「未来预定发布」的来源。
    """
    import json

    key = f"{brand}:{past_days}:{future_days}"
    cached = get_rank_cache("brand", key, ttl=BRAND_CACHE_TTL)
    if cached:
        try:
            return json.loads(cached)
        except ValueError:
            logger.debug("厂牌缓存解析失败，重新抓取")

    from app.modules.ladysite.brands import BrandUnreachable, crawl_range

    try:
        found = crawl_range(brand, past_days=past_days, future_days=future_days)
    except BrandUnreachable:
        # 交给上层转成明确的错误提示，不能静默当成"没有作品"
        raise
    except Exception as exc:
        logger.warning(f"厂牌 {brand} 抓取失败: {exc}")
        return []

    codes = [item["code"] for item in found]
    items = enrich_codes(codes)

    # 官网日期页给的发行日比详情页可靠，且预定发布的作品详情页可能还没上线
    dates = {item["code"]: item["release_date"] for item in found}
    today = datetime.now().strftime("%Y-%m-%d")
    for item in items:
        day = dates.get(item.get("code"), "")
        if day:
            item["release_date"] = day
            item["upcoming"] = day > today

    items.sort(key=lambda i: i.get("release_date") or "", reverse=True)

    if items:
        set_rank_cache("brand", key, json.dumps(items, ensure_ascii=False, default=str))
    return items


# ======================================================================
# 通知
# ======================================================================
def _code_display(code: str) -> tuple[str, str]:
    """返回 (标题, 图片地址)。"""
    with session_scope() as session:
        row = session.get(Code, code)
        if row is None:
            return code, ""
        title = row.cn_title or row.title or ""
        return (f"{code} {title}".strip(), row.banner or row.poster or "")


def send_message(text: str) -> int:
    return notify.broadcast_text(text)


def send_subscribe_message(code: str) -> int:
    title, photo = _code_display(code)
    text = f"📌 已订阅\n{title}"
    return notify.broadcast_photo(photo, text, title) if photo else notify.broadcast_text(text)


def send_subscribe_actor_message(name: str) -> int:
    return notify.broadcast_text(f"👤 已订阅演员\n{name}")


def send_downloading_message(code: str, torrent: Torrent | None = None) -> int:
    title, photo = _code_display(code)
    detail = ""
    if torrent is not None:
        size_gb = torrent.size_mb / 1024
        detail = (
            f"\n站点: {torrent.display_site}  "
            f"大小: {size_gb:.2f}GB  做种: {torrent.seeders}"
        )
    text = f"⬇️ 开始下载\n{title}{detail}"
    return notify.broadcast_photo(photo, text, title) if photo else notify.broadcast_text(text)


def send_downloaded_message(code: str) -> int:
    title, photo = _code_display(code)
    text = f"✅ 下载完成\n{title}"
    return notify.broadcast_photo(photo, text, title) if photo else notify.broadcast_text(text)


def send_complete_message(code: str) -> int:
    title, _ = _code_display(code)
    return notify.broadcast_text(f"🎬 已入库\n{title}")


def send_brush_message(text: str) -> int:
    return notify.broadcast_text(text)


def reply_text_msg(text: str, message_id: int = 0, chat_id: str = "") -> bool:
    """回复 Telegram 消息。"""
    from app.modules.notify.telegram import TelegramNotifier
    notifier = TelegramNotifier()
    if not notifier.enabled:
        return False
    if message_id:
        return notifier.reply_text_message(text, message_id, chat_id)
    return notifier.send_text_message(text, chat_id)


# ======================================================================
# 统计
# ======================================================================
def dashboard_stats() -> dict:
    """仪表盘数据。

    各状态的计数用一次 GROUP BY 拿全，原先是每个状态各扫一遍 code 表。
    """
    with session_scope() as session:
        rows = session.execute(
            select(Code.status, func.count()).group_by(Code.status)
        ).all()
        by_status = {status: total for status, total in rows}

        return {
            "total": sum(by_status.values()),
            "subscribed": by_status.get(CodeStatus.SUBSCRIBED, 0),
            "downloading": by_status.get(CodeStatus.DOWNLOADING, 0),
            "downloaded": by_status.get(CodeStatus.DOWNLOADED, 0),
            "completed": by_status.get(CodeStatus.COMPLETED, 0),
            "actors": session.scalar(select(func.count()).select_from(Actor)) or 0,
            "history": session.scalar(select(func.count()).select_from(History)) or 0,
        }


def _cache_key(namespace: str, key: str) -> str:
    return f"cinefold:{namespace}:{key}"


def get_rank_cache(namespace: str, key: str, ttl: int = 0) -> str | None:
    """读缓存。ttl > 0 时，落库的快照超过该秒数即视为失效。

    Redis 自己会过期，数据库缓存没有这个机制，得靠 create_time 判断，
    否则榜单会一直返回第一次抓到的那份快照。
    """
    from app.core import redis as redis_cache

    cached = redis_cache.get(_cache_key(namespace, key))
    if cached is not None:
        return cached

    from app.database.models import Cache
    with session_scope() as session:
        row = session.scalar(
            select(Cache).where(Cache.namespace == namespace, Cache.key == key)
        )
        if row is None:
            return None
        if ttl > 0 and row.create_time:
            age = (datetime.now() - row.create_time).total_seconds()
            if age > ttl:
                return None
        return row.content


def set_rank_cache(namespace: str, key: str, content: str) -> None:
    from app.core import redis as redis_cache

    # Redis 写入成功就不再落库，榜单快照本身是可重建的
    if redis_cache.set(_cache_key(namespace, key), content):
        return

    from app.database.models import Cache
    with session_scope() as session:
        row = session.scalar(
            select(Cache).where(Cache.namespace == namespace, Cache.key == key)
        )
        if row is None:
            session.add(Cache(namespace=namespace, key=key, content=content))
        else:
            row.content = content
            row.create_time = datetime.now()
