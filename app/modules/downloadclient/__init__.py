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
    # 迅雷未实现，调用方需先探测该方法是否存在
    def control_torrent(self, action: str, hashes: Sequence[str]) -> list[str]: ...
    def list_torrent_files(self, hashes: Sequence[str]) -> list[str]: ...
    def find_torrents_by_path(self, paths: Sequence[str]) -> dict[str, list[str]]: ...


def find_torrents_by_path(paths: Sequence[str]) -> dict[str, list[str]]:
    """按文件路径反查种子 hash，所有已配置下载器都问一遍。

    返回 {文件路径: [hash, ...]}。一个文件对应多个 hash 是正常的 ——
    转种场景下同一份文件被多个站的种子共用。

    这是 code → History → hash 之外的第二条查种子的路：手动放进监控目录的
    文件，code 是从文件名生成的，跟 History 里登记的番号对不上，查不到种子。
    下载器手里才有「这个文件属于哪个种子」的权威答案。
    """
    if not paths:
        return {}

    merged: dict[str, list[str]] = {}
    for name in list_configured_clients():
        client = get_download_client(name)
        if client is None:
            continue
        finder = getattr(client, "find_torrents_by_path", None)
        if finder is None:
            continue  # 该下载器未实现反查，跳过
        try:
            for path, hashes in finder(paths).items():
                bucket = merged.setdefault(path, [])
                for h in hashes:
                    if h not in bucket:
                        bucket.append(h)
        except Exception as exc:
            logger.warning(f"{name} 按路径反查种子异常: {exc}")

    return merged


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
