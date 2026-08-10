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
    """任一媒体库中已存在该番号即返回 True。"""
    return any(server.search(keyword) for server in get_media_servers())
