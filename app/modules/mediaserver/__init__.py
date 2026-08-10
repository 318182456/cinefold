"""媒体服务器工厂。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.core.config import get_settings


@runtime_checkable
class MediaServer(Protocol):
    def search(self, keyword: str) -> bool: ...


def get_media_servers() -> list[MediaServer]:
    """返回所有已配置的媒体服务器。"""
    settings = get_settings()
    servers: list[MediaServer] = []

    if settings.emby_url and settings.emby_api_key:
        from app.modules.mediaserver.emby import Emby
        servers.append(Emby())

    if settings.jellyfin_url and settings.jellyfin_api_key:
        from app.modules.mediaserver.jellyfin import Jellyfin
        servers.append(Jellyfin())

    if settings.plex_url and settings.plex_token:
        from app.modules.mediaserver.plex import Plex
        servers.append(Plex())

    return servers


def exists_in_any(keyword: str) -> bool:
    """任一媒体库中已存在该番号即返回 True。

    多个库并发查：串行时任一库不可达都要等满超时，三个库就是三倍。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    servers = get_media_servers()
    if not servers:
        return False
    if len(servers) == 1:
        return _safe_search(servers[0], keyword)

    with ThreadPoolExecutor(max_workers=len(servers)) as pool:
        futures = [pool.submit(_safe_search, s, keyword) for s in servers]
        for future in as_completed(futures):
            if future.result():
                # 已经确认存在，剩下的结果不再关心
                for pending in futures:
                    pending.cancel()
                return True
    return False


def _safe_search(server: MediaServer, keyword: str) -> bool:
    """单库查询，异常按"不存在"处理——宁可多下也别漏下。"""
    from loguru import logger

    try:
        return bool(server.search(keyword))
    except Exception as exc:
        logger.debug(f"媒体库查询 {keyword} 失败: {exc}")
        return False
