"""定时任务调度。

所有任务通过 APScheduler 按 crontab 触发，也可由 API 手动触发。
"""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from app.core.config import get_settings

_scheduler: BackgroundScheduler | None = None


# ======================================================================
# 任务实现
# ======================================================================
def run_codes_task() -> int:
    """订阅下载任务。"""
    from app import services
    logger.info("[任务] 开始执行订阅下载")
    return services.run_sub_task()


def run_actors() -> int:
    """演员订阅任务。"""
    from app import services
    logger.info("[任务] 开始执行演员订阅")
    return services.run_run_actor()


def sub_rank() -> int:
    """榜单订阅任务。"""
    from app import services
    settings = get_settings()
    if not settings.rank_page:
        logger.debug("[任务] RANK_PAGE 为 0，跳过榜单订阅")
        return 0

    try:
        from app.modules import ladysite
        codes = ladysite.get_rank_codes(settings.rank_type, settings.rank_page)
    except (ImportError, AttributeError):
        logger.debug("[任务] 资源站模块未接入，跳过榜单订阅")
        return 0

    if not codes:
        return 0

    count = sum(1 for code in codes if services.subscribe_code(code))
    logger.info(f"[任务] 榜单订阅新增 {count} 个")
    return count


def sub_brands() -> int:
    """厂牌订阅任务。BRAND_SUBSCRIBE 为空时不执行。

    与"同步厂牌"分开：那个只把番号收进库供浏览，这个才会订阅并进下载流程。
    """
    from app import services
    from app.modules.ladysite.brands import BRANDS, crawl_recent

    settings = get_settings()
    setting = (settings.brand_subscribe or "").strip().lower()
    if not setting:
        logger.debug("[任务] BRAND_SUBSCRIBE 为空，跳过厂牌订阅")
        return 0

    if setting == "all":
        brands = list(BRANDS)
    else:
        brands = [b.strip() for b in setting.split(",") if b.strip() in BRANDS]

    # 只订阅已发布的，预定发布的还没有资源可找
    days = max(settings.brand_subscribe_days, 1)

    count = 0
    for brand in brands:
        try:
            codes = crawl_recent(brand, days=days)
        except Exception as exc:
            # 单个厂牌不通不该影响其余厂牌
            logger.warning(f"[任务] 厂牌 {brand} 抓取失败: {exc}")
            continue
        count += sum(1 for code in codes if services.subscribe_code(code))

    logger.info(f"[任务] 厂牌订阅新增 {count} 个")
    return count


def sync_hot() -> int:
    """同步热门榜单。"""
    return _run_ladysite_task("sync_hot", "同步热门")


def sync_brands() -> int:
    """同步厂牌新片。"""
    return _run_ladysite_task("sync_brands", "同步厂牌")


def sync_actors() -> int:
    """同步演员信息。"""
    from app import services
    logger.info("[任务] 开始补全演员信息")
    return services.fill_lack_actors()


# 页面接口的默认取值，预热必须与其一致，否则算出来的缓存键对不上
_WARM_BRAND_RANGE = (7, 14)
_WARM_RANK_TYPES = ("daily", "weekly", "monthly")


def warm_page_cache() -> int:
    """预热推荐、榜单、厂牌三个页面的缓存。

    这三个页面首次打开要跨境抓一遍再补详情，实测几十秒，用户只能干等。
    提前在后台抓好，打开就是命中缓存。

    与 sync_hot / sync_brands 的区别：那两个任务只把番号入库，
    不写页面读的那份缓存，所以缓存过期后用户照样要等。
    """
    from app import services

    warmed = 0

    for rank_type in _WARM_RANK_TYPES:
        try:
            if services.get_rank_items(rank_type):
                warmed += 1
        except Exception as exc:
            logger.warning(f"[任务] 预热榜单 {rank_type} 失败: {exc}")

    # 推荐页读库不抓站，本身就快，不需要预热

    from app.modules.ladysite.brands import BRANDS

    setting = (get_settings().brand_type or "").strip().lower()
    if setting == "all":
        brands = list(BRANDS)
    elif setting:
        brands = [b.strip() for b in setting.split(",") if b.strip() in BRANDS]
    else:
        # 未配置 BRAND_TYPE 时只预热第一个厂牌：厂牌页默认就打开它，
        # 而把 9 个站全抓一遍代价太大，不该在用户没选的情况下默认做
        brands = list(BRANDS)[:1]

    past_days, future_days = _WARM_BRAND_RANGE
    for brand in brands:
        try:
            if services.get_brand_items(brand, past_days=past_days, future_days=future_days):
                warmed += 1
        except Exception as exc:
            # 单个厂牌不通不该影响其余厂牌
            logger.warning(f"[任务] 预热厂牌 {brand} 失败: {exc}")

    logger.info(f"[任务] 页面缓存预热完成，{warmed} 项")
    return warmed


def sync_news() -> int:
    """同步新片。"""
    return _run_ladysite_task("sync_news", "同步新片")


def fill_empty_banner() -> int:
    """补全缺图的番号。"""
    from app import services
    logger.info("[任务] 开始补全番号详情")
    return services.fill_lack_codes()


def fill_subtitles() -> int:
    """给媒体库里没字幕的影片补抓。开关（SUBTITLE_ENABLED）关着时直接返回。"""
    from app.services import subtitle
    logger.info("[任务] 开始补抓字幕")
    return subtitle.fill_lack_subtitles()


def fill_reviews() -> int:
    """给媒体库里没影评的影片补生成。开关（REVIEW_ENABLED）关着时直接返回。

    顺带把生成过但没写进 NFO 的补写出去 —— 刮削工具重刮会把 plot 冲掉，
    这轮回填。
    """
    from app.services import review
    logger.info("[任务] 开始补生成影评")
    return review.fill_lack_reviews()


def translate_titles() -> int:
    """翻译缺中文标题的番号。"""
    from app import services
    return services.translate_codes()


def pt_wait() -> int:
    """同步下载器状态，顺带给正在下的种子补标广告文件。

    补标合在这里而不是单开一个任务：两者都要遍历 DOWNLOADING 的种子，
    合起来能省一半下载器请求。推送磁链时元数据往往还没到、挑不出广告，
    这一轮才是实际生效的地方。
    """
    from app import services
    completed = services.sync_download_status()
    try:
        services.unwant_junk_for_downloading()
    except Exception as exc:
        # 补标失败不该让状态同步的结果丢掉
        logger.warning(f"[任务] 补标广告文件失败: {exc}")
    return completed


def transfer_seeds() -> int:
    """转移做种：把 qb 里已下载完的种子交给 tr 继续做种。

    开关（SEED_TRANSFER_ENABLED）关着时立即返回，不打下载器接口。
    """
    from app.services.seedtransfer import run_auto_transfer

    try:
        return run_auto_transfer()
    except Exception as exc:
        logger.exception(f"[任务] 转移做种失败: {exc}")
        return 0


def sync_watch_dirs() -> int:
    """监控目录全量对账。

    watchdog 已经在实时响应了，这个任务是兜底：inotify 事件在 Docker 绑定
    挂载、NFS/SMB 上会丢，容器重启期间的变化也没人看见。只有周期性全量
    对账能把状态收敛回正确。

    返回处理的规则数。
    """
    if not get_settings().watchdir_auto_sync:
        logger.debug("[任务] 自动同步已关闭（WATCHDIR_AUTO_SYNC=false），跳过对账")
        return 0

    from app.services.watchdir import backfill_torrents, sync_all

    results = sync_all()
    if not results:
        return 0

    linked = sum(len(r.linked) for r in results)
    unlinked = sum(len(r.unlinked) for r in results)
    moved = sum(len(r.moved) for r in results)
    held = sum(len(r.held) for r in results)
    reverse = sum(len(r.reverse_deleted) for r in results)
    if linked or unlinked or moved or reverse:
        logger.info(
            f"[任务] 监控目录对账 —— 新建 {linked}，删除 {unlinked}，"
            f"移动 {moved}，扣留 {held}，反向清理 {reverse}"
        )

    # 建链接时可能还查不到种子（下载未完成、完成后才移入、事后做种），
    # 每轮补一次。种子信息只有趁它还在下载器里才拿得到
    try:
        added = backfill_torrents()
        if added:
            logger.info(f"[任务] 补登记 {added} 个种子")
    except Exception as exc:
        logger.warning(f"[任务] 补登记种子异常: {exc}")

    return len(results)


def refresh_link_sizes() -> int:
    """回填媒体关联的文件大小与字幕状态。

    这两列不入库就没法排序筛选（大小得 stat、字幕得列目录，都下推不成
    SQL）。新登记的记录在登记时就带上了值，这个任务管的是存量 ——
    升级上来的库里全是空的。

    每轮只探一批，把回填摊到多轮里。库大又挂在 NAS 上时，一次全量能跑
    到分钟级，摊开跑对页面响应更友好；探完一轮就不再重复探。
    """
    from app.services.medialink import refresh_sizes

    try:
        result = refresh_sizes()
    except Exception as exc:
        logger.exception(f"[任务] 媒体关联体积回填失败: {exc}")
        return 0

    return result.get("probed", 0)


def scan_orphans() -> int:
    """扫描「下载侧已删、媒体库侧仍在」的关联。

    只报告不删除。放进定时任务是为了 source_gone_time 这个时间戳 ——
    它记的是「首次发现源文件消失」，只有定期扫才准。全靠页面触发的话，
    一个月不打开页面，那一个月里删掉的文件全都会记成打开页面的那一刻。
    """
    from app.services.orphan import scan_orphans as _scan

    try:
        items = _scan()
    except Exception as exc:
        logger.exception(f"[任务] 孤儿关联扫描失败: {exc}")
        return 0

    if items:
        logger.info(
            f"[任务] 孤儿关联 {len(items)} 条 —— "
            f"源文件已删 {sum(1 for i in items if i['source_gone'])}，"
            f"种子已删 {sum(1 for i in items if i['torrent_gone'])}"
        )
    return len(items)


def cache_photos() -> int:
    """图片本地化。未开启时跳过。"""
    settings = get_settings()
    if not settings.enable_photo_cache:
        return 0

    from app import services
    logger.info("[任务] 开始缓存封面")
    return services.cache_lack_photos()


def save_image(url: str, target: str = "") -> str:
    """下载单张图片到本地，返回相对路径。"""
    import httpx
    from pathlib import Path
    from app.utils import get_image_suffix_from_url, safe_map_url_to_filesystem

    if not url:
        return ""

    pic_dir = Path("/data/pics")
    relative = target or safe_map_url_to_filesystem(url)
    if not Path(relative).suffix:
        relative += get_image_suffix_from_url(url)

    dest = pic_dir / relative
    if dest.exists():
        return relative

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = client.get(url, headers={"Referer": "https://www.javbus.com/"})
            response.raise_for_status()
            dest.write_bytes(response.content)
        return relative
    except Exception as exc:
        logger.warning(f"保存图片失败 {url}: {exc}")
        return ""


def auto_update() -> int:
    """自动更新。开关关着时立即返回，不产生任何网络请求。

    真升级会重启进程，所以这个任务的返回值多半没机会被记下来。
    """
    try:
        from app.services.upgrade import auto_upgrade
        return auto_upgrade()
    except Exception as exc:
        logger.warning(f"[任务] 自动更新检查失败: {exc}")
        return 0


def _run_ladysite_task(func_name: str, label: str) -> int:
    """资源站任务的统一入口，模块未接入时静默跳过。"""
    try:
        from app.modules import ladysite
        func = getattr(ladysite, func_name)
    except (ImportError, AttributeError):
        logger.debug(f"[任务] 资源站模块未接入，跳过{label}")
        return 0

    logger.info(f"[任务] 开始{label}")
    try:
        return func() or 0
    except Exception as exc:
        logger.exception(f"[任务] {label}失败: {exc}")
        return 0


def import_crawler_db() -> int:
    """从外部爬虫库导入番号情报。DSN 没配时直接返回。

    只补空字段、不改 status —— 导入是为了充实番号库供浏览和搜索，
    不该让几万条番号悄悄进下载流程。要下载还是手动订阅。
    """
    settings = get_settings()
    if not settings.crawler_db_dsn:
        logger.debug("[任务] CRAWLER_DB_DSN 未配置，跳过爬虫库导入")
        return 0

    from app.modules import crawlerdb

    logger.info("[任务] 开始从爬虫库导入番号")
    try:
        return crawlerdb.import_all()
    except crawlerdb.CrawlerDBError as exc:
        # 对方的库不归我们管，连不上是常态。记 warning 让它下轮再试，
        # 不要抛出去——异常会让 APScheduler 把这个任务整个移除
        logger.warning(f"[任务] 爬虫库导入失败: {exc}")
        return 0


# ======================================================================
# 任务注册表
# ======================================================================
JOBS: dict[str, dict] = {
    "run_codes_task": {"func": run_codes_task, "name": "订阅下载", "cron_key": "download_schedule_time"},
    "run_actors": {"func": run_actors, "name": "演员订阅", "cron_key": "actor_schedule_time"},
    "sub_rank": {"func": sub_rank, "name": "榜单订阅", "cron_key": "rank_schedule_time"},
    "sub_brands": {"func": sub_brands, "name": "厂牌订阅", "cron_key": "brand_schedule_time"},
    "sync_hot": {"func": sync_hot, "name": "同步热门", "cron_key": "sync_hot_time"},
    "sync_brands": {"func": sync_brands, "name": "同步厂牌", "cron_key": "sync_brands_time"},
    "sync_actors": {"func": sync_actors, "name": "同步演员", "cron_key": "sync_actors_time"},
    "sync_news": {"func": sync_news, "name": "同步新片", "cron_key": "sync_news"},
    "fill_empty_banner": {"func": fill_empty_banner, "name": "补全缺图", "cron_key": "fill_empty_image_time"},
    "warm_page_cache": {"func": warm_page_cache, "name": "预热页面缓存", "cron_key": "warm_cache_time"},
    # 默认凌晨跑：每部片都要跨境请求，白天跑会跟抓取任务抢过盾服务
    "fill_subtitles": {"func": fill_subtitles, "name": "补抓字幕", "cron_key": "subtitle_fill_time"},
    "fill_reviews": {"func": fill_reviews, "name": "补生成影评", "cron_key": "review_fill_time"},
    # cron 默认为空，配了 CRAWLER_DB_TIME 才排班
    "import_crawler_db": {"func": import_crawler_db, "name": "导入爬虫库", "cron_key": "crawler_db_time"},
}

# 固定间隔任务，不走 crontab
INTERVAL_JOBS: dict[str, dict] = {
    "pt_wait": {"func": pt_wait, "name": "同步下载状态", "minutes": 5},
    "translate_titles": {"func": translate_titles, "name": "翻译标题", "minutes": 30},
    "cache_photos": {"func": cache_photos, "name": "缓存封面", "minutes": 20},
    # watchdog 实时监听的兜底。NAS / Docker 绑定挂载上 inotify 事件经常收不到，
    # 那种环境下这个兜底才是实际起作用的路径，所以间隔做成可配置
    # （WATCHDIR_SYNC_INTERVAL），minutes 只是没配置时的默认值
    "sync_watch_dirs": {
        "func": sync_watch_dirs, "name": "监控目录对账", "minutes": 30,
        "interval_key": "watchdir_sync_interval",
    },
    # 只读扫描，不删任何东西。频率不用高 —— 它维护的是「首次发现消失」的
    # 时间戳，一小时的精度对这个用途完全够，而每轮要拉下载器全量种子清单
    "scan_orphans": {
        "func": scan_orphans, "name": "孤儿关联扫描", "minutes": 60,
    },
    # 存量记录的体积/字幕回填。每轮一批，探完就不再动 ——
    # 全填满之后这个任务基本是空转，留着是为了接住新增的漏网记录
    "refresh_link_sizes": {
        "func": refresh_link_sizes, "name": "媒体关联体积回填", "minutes": 30,
    },
    # 开关（SEED_TRANSFER_ENABLED）关着时直接返回，不打下载器接口
    "transfer_seeds": {
        "func": transfer_seeds, "name": "转移做种", "minutes": 60,
        "interval_key": "seed_transfer_interval",
    },
    # 开关（AUTO_UPDATE_ENABLED）关着时直接返回，不打 API
    "auto_update": {
        "func": auto_update, "name": "自动更新", "minutes": 360,
        "interval_key": "update_check_interval",
    },
}


# ======================================================================
# 调度器管理
# ======================================================================
def _build_jobstores() -> dict:
    """开启 REDIS_JOB_STORE 且 Redis 可用时，任务状态持久化到 Redis。

    否则返回空配置，APScheduler 用默认的内存 jobstore。
    """
    settings = get_settings()
    if not settings.redis_job_store:
        return {}

    from app.core import redis as redis_cache
    client = redis_cache.get_client()
    if client is None:
        logger.warning("已开启 REDIS_JOB_STORE 但 Redis 不可用，任务状态仅存内存")
        return {}

    try:
        from apscheduler.jobstores.redis import RedisJobStore
    except ImportError:
        logger.warning("未安装 redis 包，APScheduler 任务状态仅存内存")
        return {}

    try:
        # jobstore 存的是 pickle 二进制，必须关掉 decode_responses
        kwargs = dict(client.connection_pool.connection_kwargs)
        kwargs.pop("decode_responses", None)
        store = RedisJobStore(
            jobs_key="cinefold:jobs",
            run_times_key="cinefold:job_run_times",
            **kwargs,
        )
        logger.info("APScheduler 任务状态持久化到 Redis")
        return {"default": store}
    except Exception as exc:
        logger.warning(f"Redis jobstore 初始化失败，任务状态仅存内存: {exc}")
        return {}


def start_scheduler() -> BackgroundScheduler:
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        return _scheduler

    settings = get_settings()
    _scheduler = BackgroundScheduler(
        timezone="Asia/Shanghai",
        jobstores=_build_jobstores(),
    )

    for job_id, meta in JOBS.items():
        cron = getattr(settings, meta["cron_key"], "")
        if not cron:
            continue
        try:
            _scheduler.add_job(
                meta["func"],
                CronTrigger.from_crontab(cron, timezone="Asia/Shanghai"),
                id=job_id,
                name=meta["name"],
                replace_existing=True,
                max_instances=1,
                misfire_grace_time=3600,
            )
        except ValueError as exc:
            logger.error(f"任务 {job_id} 的 cron 表达式非法 ({cron}): {exc}")

    for job_id, meta in INTERVAL_JOBS.items():
        minutes = meta["minutes"]
        # 配了 interval_key 的任务，间隔取设置里的值。非正数一律退回默认 ——
        # 0 会让 APScheduler 疯狂触发，负数直接报错
        key = meta.get("interval_key")
        if key:
            configured = getattr(settings, key, 0)
            if isinstance(configured, int) and configured > 0:
                minutes = configured
            elif configured:
                logger.warning(
                    f"任务 {job_id} 的间隔配置非法（{key}={configured}），"
                    f"退回默认 {minutes} 分钟"
                )

        _scheduler.add_job(
            meta["func"],
            "interval",
            minutes=minutes,
            id=job_id,
            name=meta["name"],
            replace_existing=True,
            max_instances=1,
        )

    _scheduler.start()
    logger.info(f"调度器已启动，共 {len(_scheduler.get_jobs())} 个任务")
    return _scheduler


def restart_scheduler() -> BackgroundScheduler:
    """配置变更后重建调度器。"""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        # 持久化的 jobstore 会留下上一轮的任务，先清干净再重建
        try:
            _scheduler.remove_all_jobs()
        except Exception as exc:
            logger.warning(f"清理旧任务失败: {exc}")
        _scheduler.shutdown(wait=False)
    _scheduler = None
    get_settings(reload=True)

    # 配置可能改了 REDIS_URL，重新探测连接
    from app.core import redis as redis_cache
    redis_cache.reset()
    return start_scheduler()


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None


def list_jobs() -> list[dict]:
    """供 API 展示任务列表。"""
    if _scheduler is None:
        return []
    return [
        {
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
            if job.next_run_time else "",
            "trigger": str(job.trigger),
        }
        for job in _scheduler.get_jobs()
    ]


def push_job(job_id: str) -> bool:
    """手动触发任务，在后台线程执行。"""
    meta = JOBS.get(job_id) or INTERVAL_JOBS.get(job_id)
    if meta is None:
        logger.warning(f"未知任务 {job_id}")
        return False

    from app.utils import run_in_background
    run_in_background(meta["func"])
    logger.info(f"已手动触发任务 {meta['name']}")
    return True
