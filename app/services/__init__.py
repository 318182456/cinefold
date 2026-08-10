"""业务服务层。

编排各模块完成完整业务链路：搜种 → 过滤 → 下载 → 记录 → 通知。
本层不直接处理 HTTP，只依赖 modules 与 database。
"""
from __future__ import annotations

from datetime import datetime

from loguru import logger
from sqlalchemy import func, select

from app.core.config import get_settings
from app.database.models import Actor, Code, CodeStatus, History
from app.database.session import session_scope
from app.modules import downloadclient, mediaserver, notify, ptsite, translate
from app.schemas.torrent import Torrent
from app.utils import get_magnet_hash
from app.utils.filters import filter_torrents, sort_torrents


# ======================================================================
# 搜索
# ======================================================================
def search_torrents(code: str, use_filter: bool = True) -> list[Torrent]:
    """搜索番号对应的种子，按配置过滤排序后返回。"""
    settings = get_settings()
    torrents = ptsite.search_pt(code)
    if not torrents:
        return []

    for torrent in torrents:
        torrent.code = torrent.code or code

    if use_filter:
        torrents = filter_torrents(torrents, settings.default_filter)

    site_priority = [site.name for site in ptsite.get_sites()]
    return sort_torrents(torrents, settings.default_sort, site_priority)


def find_torrent(code: str) -> Torrent | None:
    """取排序后最优的一个种子。"""
    results = search_torrents(code)
    return results[0] if results else None


def search_code(keyword: str, limit: int = 50) -> list[dict]:
    """在本地库中按番号或标题搜索。"""
    with session_scope() as session:
        pattern = f"%{keyword}%"
        rows = session.scalars(
            select(Code)
            .where(Code.code.like(pattern) | Code.title.like(pattern) | Code.cn_title.like(pattern))
            .order_by(Code.release_date.desc())
            .limit(limit)
        ).all()
        return [row.to_dict() for row in rows]


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
def is_exist_server(code: str) -> bool:
    """番号是否已在任一媒体库中。"""
    return mediaserver.exists_in_any(code)


# ======================================================================
# 下载
# ======================================================================
def download_torrent(code: str, torrent: Torrent | None = None) -> bool:
    """下载指定番号。torrent 为空时自动搜索最优种子。"""
    settings = get_settings()

    # 已入库的直接跳过，避免重复占用带宽
    if settings.enable_auto_complete and is_exist_server(code):
        logger.info(f"[{code}] 媒体库中已存在，跳过下载")
        _update_code_status(code, CodeStatus.COMPLETED)
        return False

    torrent = torrent or find_torrent(code)
    if torrent is None:
        logger.info(f"[{code}] 未搜到符合条件的种子")
        return False

    if _is_downloaded(code):
        logger.info(f"[{code}] 已下载过，跳过")
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
    """把某演员在 limit_date 之后的作品标记为已订阅。"""
    with session_scope() as session:
        query = select(Code).where(
            Code.casts.like(f"%{actor_name}%"),
            Code.status == CodeStatus.NONE,
        )
        if limit_date:
            query = query.where(Code.release_date >= limit_date)

        rows = session.scalars(query).all()
        for row in rows:
            row.status = CodeStatus.SUBSCRIBED

        if rows:
            logger.info(f"[{actor_name}] 新增订阅 {len(rows)} 个番号")
        return len(rows)


def subscribe_code(code: str) -> bool:
    """订阅单个番号。"""
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


def subscribe_actor(name: str, limit_date: str = "") -> bool:
    with session_scope() as session:
        row = session.get(Actor, name)
        if row is None:
            session.add(Actor(name=name, limit_date=limit_date or None))
        else:
            row.limit_date = limit_date or row.limit_date
    send_subscribe_actor_message(name)
    return True


def cancel_actor(name: str) -> bool:
    with session_scope() as session:
        row = session.get(Actor, name)
        if row is None:
            return False
        session.delete(row)
    return True


# ======================================================================
# 下载状态同步
# ======================================================================
def sync_download_status() -> int:
    """查询下载器，把已完成的任务更新为下载完成并通知。"""
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
    for state in states:
        if not state.get("completed"):
            continue
        code = hash_to_code.get(state.get("hash", ""))
        if not code:
            continue
        _update_code_status(code, CodeStatus.DOWNLOADED)
        _update_save_path(state.get("hash", ""), state.get("save_path", ""))
        send_downloaded_message(code)
        completed += 1

    if completed:
        logger.info(f"{completed} 个任务下载完成")
    return completed


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

    count = 0
    for code, title in pending:
        translated = translate_title(title)
        if not translated:
            continue
        with session_scope() as session:
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
    if not codes:
        return 0
    try:
        from app.modules import ladysite
    except ImportError:
        logger.debug("资源站模块未接入，跳过补全")
        return 0

    count = 0
    for code in codes:
        detail = ladysite.get_code_detail(code)
        if not detail:
            continue
        with session_scope() as session:
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


def fill_lack_actors(limit: int = 50) -> int:
    with session_scope() as session:
        names = session.scalars(
            select(Actor.name)
            .where((Actor.photo.is_(None)) | (Actor.photo == ""))
            .limit(limit)
        ).all()
    return fill_lack_actors_by_list(list(names))


def fill_lack_actors_by_list(names: list[str]) -> int:
    if not names:
        return 0
    try:
        from app.modules import ladysite
    except ImportError:
        return 0

    count = 0
    for name in names:
        photo = ladysite.get_actor_photo(name)
        if not photo:
            continue
        with session_scope() as session:
            row = session.get(Actor, name)
            if row is not None:
                row.photo = photo
                count += 1
    return count


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
        detail = f"\n站点: {torrent.site}  大小: {size_gb:.2f}GB  做种: {torrent.seeders}"
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
    """仪表盘数据。"""
    with session_scope() as session:
        def count_where(*conditions):
            return session.scalar(
                select(func.count()).select_from(Code).where(*conditions)
            ) or 0

        return {
            "total": session.scalar(select(func.count()).select_from(Code)) or 0,
            "subscribed": count_where(Code.status == CodeStatus.SUBSCRIBED),
            "downloading": count_where(Code.status == CodeStatus.DOWNLOADING),
            "downloaded": count_where(Code.status == CodeStatus.DOWNLOADED),
            "completed": count_where(Code.status == CodeStatus.COMPLETED),
            "actors": session.scalar(select(func.count()).select_from(Actor)) or 0,
            "history": session.scalar(select(func.count()).select_from(History)) or 0,
        }


def _cache_key(namespace: str, key: str) -> str:
    return f"byte-muse:{namespace}:{key}"


def get_rank_cache(namespace: str, key: str) -> str | None:
    from app.core import redis as redis_cache

    cached = redis_cache.get(_cache_key(namespace, key))
    if cached is not None:
        return cached

    from app.database.models import Cache
    with session_scope() as session:
        row = session.scalar(
            select(Cache).where(Cache.namespace == namespace, Cache.key == key)
        )
        return row.content if row else None


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
