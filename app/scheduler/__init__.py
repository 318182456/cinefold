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


def sync_news() -> int:
    """同步新片。"""
    return _run_ladysite_task("sync_news", "同步新片")


def fill_empty_banner() -> int:
    """补全缺图的番号。"""
    from app import services
    logger.info("[任务] 开始补全番号详情")
    return services.fill_lack_codes()


def translate_titles() -> int:
    """翻译缺中文标题的番号。"""
    from app import services
    return services.translate_codes()


def pt_wait() -> int:
    """同步下载器状态。"""
    from app import services
    return services.sync_download_status()


def cache_photos() -> int:
    """图片本地化。未开启时跳过。"""
    settings = get_settings()
    if not settings.enable_photo_cache:
        return 0
    logger.debug("[任务] 图片持久化暂未接入")
    return 0


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


# ======================================================================
# 任务注册表
# ======================================================================
JOBS: dict[str, dict] = {
    "run_codes_task": {"func": run_codes_task, "name": "订阅下载", "cron_key": "download_schedule_time"},
    "run_actors": {"func": run_actors, "name": "演员订阅", "cron_key": "actor_schedule_time"},
    "sub_rank": {"func": sub_rank, "name": "榜单订阅", "cron_key": "rank_schedule_time"},
    "sync_hot": {"func": sync_hot, "name": "同步热门", "cron_key": "sync_hot_time"},
    "sync_brands": {"func": sync_brands, "name": "同步厂牌", "cron_key": "sync_brands_time"},
    "sync_actors": {"func": sync_actors, "name": "同步演员", "cron_key": "sync_actors_time"},
    "sync_news": {"func": sync_news, "name": "同步新片", "cron_key": "sync_news"},
    "fill_empty_banner": {"func": fill_empty_banner, "name": "补全缺图", "cron_key": "fill_empty_image_time"},
}

# 固定间隔任务，不走 crontab
INTERVAL_JOBS: dict[str, dict] = {
    "pt_wait": {"func": pt_wait, "name": "同步下载状态", "minutes": 5},
    "translate_titles": {"func": translate_titles, "name": "翻译标题", "minutes": 30},
}


# ======================================================================
# 调度器管理
# ======================================================================
def start_scheduler() -> BackgroundScheduler:
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        return _scheduler

    settings = get_settings()
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

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
        _scheduler.add_job(
            meta["func"],
            "interval",
            minutes=meta["minutes"],
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
        _scheduler.shutdown(wait=False)
    _scheduler = None
    get_settings(reload=True)
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
