"""从指定 Emby 用户读取观看状态；全量成功后才替换快照。"""
from __future__ import annotations

import threading
import hashlib
from datetime import datetime
from pathlib import PureWindowsPath

import httpx
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.database.models import Code, EmbyWatch, MediaLink
from app.database.session import session_scope
from app.utils import find_serial_numbers

_lock = threading.Lock()


def users() -> list[dict]:
    settings = get_settings()
    if not settings.emby_url or not settings.emby_api_key:
        raise ValueError("请先在设置中配置 Emby 地址与 API Key")
    with httpx.Client(timeout=30, trust_env=False) as client:
        response = client.get(f"{settings.emby_url.rstrip('/')}/emby/Users", headers={"X-Emby-Token": settings.emby_api_key})
        response.raise_for_status()
        return [{"id": x["Id"], "name": x["Name"]} for x in response.json()]


def sync() -> dict:
    settings = get_settings()
    if not settings.emby_url or not settings.emby_api_key or not settings.emby_user_id:
        raise ValueError("请先配置 Emby 并选择观看统计用户")
    if not _lock.acquire(blocking=False):
        raise ValueError("Emby 同步正在运行")
    try:
        return _sync(settings)
    finally:
        _lock.release()


def _sync(settings) -> dict:
    items, start = [], 0
    with httpx.Client(timeout=30, trust_env=False) as client:
        while True:
            response = client.get(f"{settings.emby_url.rstrip('/')}/emby/Users/{settings.emby_user_id}/Items",
                headers={"X-Emby-Token": settings.emby_api_key}, params={"Recursive": "true", "IncludeItemTypes": "Movie",
                "EnableUserData": "true", "Fields": "Path,Genres,People,Studios,Overview,ProviderIds", "StartIndex": start, "Limit": 200})
            response.raise_for_status()
            data = response.json()
            batch = data.get("Items", [])
            items.extend(batch)
            start += len(batch)
            if start >= data.get("TotalRecordCount", start) or not batch:
                break
    matched, ignored = 0, 0
    with session_scope() as session:
        # 精确文件路径优先，名称中的单个番号其次。无法归一的条目不误匹配。
        paths = {str(PureWindowsPath(link.link_path)).casefold(): link.code for link in session.scalars(select(MediaLink))}
        known = {code.upper(): code for code in session.scalars(select(Code.code))}
        snapshots = []
        for item in items:
            path_key = str(PureWindowsPath(item.get("Path") or "")).casefold()
            candidates = find_serial_numbers(f"{PureWindowsPath(item.get('Path') or '').stem} {item.get('Name') or ''}")
            matched_codes = {known[x.upper()] for x in candidates if x.upper() in known}
            code = paths.get(path_key) or (next(iter(matched_codes)) if len(matched_codes) == 1 else None)
            if not code:
                ignored += 1
                continue
            user_data = item.get("UserData") or {}
            snapshots.append(EmbyWatch(user_id=settings.emby_user_id, item_id=str(item["Id"]), code=code,
                favorite=bool(user_data.get("IsFavorite")), server=hashlib.sha256(settings.emby_url.rstrip('/').encode()).hexdigest(),
                played=bool(user_data.get("Played")), play_count=user_data.get("PlayCount") or 0,
                position_ticks=user_data.get("PlaybackPositionTicks") or 0, runtime_ticks=item.get("RunTimeTicks") or 0,
                last_played=user_data.get("LastPlayedDate"), sync_time=datetime.now()))
            matched += 1
        # 切换用户时不混合不同用户的统计；断网、翻页失败不会清空旧结果。
        session.execute(delete(EmbyWatch))
        session.add_all(snapshots)
    return {"matched": matched, "ignored": ignored, "total": len(items)}


def set_user_state(code: str, item_id: str, action: str) -> dict:
    """用户点击后才写入 Emby；只允许操作已同步到所选用户的影片。"""
    from urllib.parse import quote
    settings = get_settings()
    if not settings.emby_url or not settings.emby_api_key or not settings.emby_user_id:
        raise ValueError("请先配置 Emby 并选择观看统计用户")
    if action not in ('favorite', 'unfavorite', 'played', 'unplayed'):
        raise ValueError("不支持的 Emby 操作")
    if not _lock.acquire(blocking=False):
        raise ValueError("Emby 正在同步或更新，请稍后重试")
    try:
        with session_scope() as session:
            row = session.get(EmbyWatch, (settings.emby_user_id, item_id))
            server = hashlib.sha256(settings.emby_url.rstrip('/').encode()).hexdigest()
            if row is None or row.code != code or row.server != server:
                raise ValueError("影片尚未与当前 Emby 用户关联，请先同步 Emby")
            resource = 'FavoriteItems' if action in ('favorite', 'unfavorite') else 'PlayedItems'
            method = 'POST' if action in ('favorite', 'played') else 'DELETE'
            url = f"{settings.emby_url.rstrip('/')}/emby/Users/{quote(settings.emby_user_id, safe='')}/{resource}/{quote(item_id, safe='')}"
            with httpx.Client(timeout=20, trust_env=False) as client:
                response = client.request(method, url, headers={'X-Emby-Token': settings.emby_api_key})
                response.raise_for_status()
                data = response.json() if response.content else {}
            if not isinstance(data, dict):
                raise RuntimeError('Emby 返回格式无效')
            if resource == 'FavoriteItems':
                row.favorite = bool(data.get('IsFavorite', action == 'favorite'))
            else:
                row.played = bool(data.get('Played', action == 'played'))
            for remote, local in (('PlaybackPositionTicks', 'position_ticks'), ('PlayCount', 'play_count'), ('LastPlayedDate', 'last_played')):
                if remote in data:
                    setattr(row, local, data[remote])
            row.sync_time = datetime.now()
            session.flush()
            return row.to_dict()
    finally:
        _lock.release()


def scheduled_sync() -> int:
    from loguru import logger
    settings = get_settings()
    if not settings.emby_user_id or not settings.emby_api_key or not settings.emby_url:
        return 0
    try:
        return sync()["matched"]
    except Exception as exc:
        logger.warning(f"Emby 观看状态同步失败: {type(exc).__name__}")
        return 0
