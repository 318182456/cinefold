"""下载器工厂。按配置选择可用的客户端。"""
from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from loguru import logger

from app.core.config import get_settings


@runtime_checkable
class DownloadClient(Protocol):
    def add_torrent(self, content: bytes, code: str = "", save_path: str = "") -> str | None: ...
    def add_torrent_by_magnet(self, magnet: str, code: str = "", save_path: str = "") -> str | None: ...
    def monitor_torrent(self, hashes: Sequence[str] | None = None) -> list[dict]: ...
    def delete_torrent(self, hashes: Sequence[str], delete_files: bool = False) -> list[str]: ...


def get_download_client(name: str = "") -> DownloadClient | None:
    """返回下载客户端实例。

    name 为空时按 qBittorrent → Transmission → 迅雷 的顺序取第一个已配置的。
    """
    settings = get_settings()
    name = (name or "").lower()

    if name in ("qbittorrent", "qb") or (not name and settings.qbittorrent_url):
        from app.modules.downloadclient.qbittorrent import QBitTorrentClient
        return QBitTorrentClient()

    if name in ("transmission", "tr") or (not name and settings.transmission_url):
        from app.modules.downloadclient.transmission import TransmissionClient
        return TransmissionClient()

    if name in ("thunder", "xunlei") or (not name and settings.thunder_url):
        from app.modules.downloadclient.thunder import Thunder
        return Thunder()

    logger.warning("未配置任何下载器")
    return None


def list_configured_clients() -> list[str]:
    settings = get_settings()
    names = []
    if settings.qbittorrent_url:
        names.append("qbittorrent")
    if settings.transmission_url:
        names.append("transmission")
    if settings.thunder_url:
        names.append("thunder")
    return names
