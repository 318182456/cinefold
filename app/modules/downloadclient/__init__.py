"""下载器工厂。按配置选择可用的客户端。"""
from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from loguru import logger

from app.core.config import get_settings


@runtime_checkable
class DownloadClient(Protocol):
    def add_torrent(self, content: bytes, code: str = "", save_path: str = "") -> str | None: ...
    def add_torrent_by_magnet(self, magnet: str, code: str = "", save_path: str = "") -> str | None: ...
    # all_categories 仅 qBittorrent 有意义（它会按 QBITTORRENT_CATEGORY 过滤）；
    # 传 True 表示不按分类过滤，返回下载器里的全部任务
    def monitor_torrent(
        self, hashes: Sequence[str] | None = None, all_categories: bool = False
    ) -> list[dict]: ...
    def delete_torrent(self, hashes: Sequence[str], delete_files: bool = False) -> list[str]: ...
    # 迅雷未实现，调用方需先探测该方法是否存在
    def control_torrent(self, action: str, hashes: Sequence[str]) -> list[str]: ...
    def list_torrent_files(self, hashes: Sequence[str]) -> list[str]: ...
    # 带大小的文件清单，返回 [{path, size}]。挑广告文件要按体积判断，
    # 迅雷未实现，调用方需先探测该方法是否存在
    def list_torrent_files_detailed(self, torrent_hash: str) -> list[dict]: ...
    # paths 传 None 表示不过滤，返回下载器里全部文件的映射
    def find_torrents_by_path(self, paths: Sequence[str] | None) -> dict[str, list[str]]: ...
    # 迅雷未实现，调用方需先探测该方法是否存在
    def unwant_torrent_files(
        self, torrent_hash: str, paths: Sequence[str]
    ) -> tuple[int, int]: ...
    # 上传限速，单位字节/秒，<=0 表示取消限速。
    # 迅雷未实现，调用方需先探测该方法是否存在
    def set_upload_limit(self, hashes: Sequence[str], limit_bytes: int) -> list[str]: ...


def find_torrents_by_path(paths: Sequence[str]) -> dict[str, list[str]]:
    """按文件路径反查种子 hash，所有已配置下载器都问一遍。

    返回 {文件路径: [hash, ...]}。一个文件对应多个 hash 是正常的 ——
    转种场景下同一份文件被多个站的种子共用。

    这是 code → History → hash 之外的第二条查种子的路：手动放进监控目录的
    文件，code 是从文件名生成的，跟 History 里登记的番号对不上，查不到种子。
    下载器手里才有「这个文件属于哪个种子」的权威答案。
    """
    merged, _ = find_torrents_by_path_checked(paths)
    return merged


def find_torrents_by_path_checked(
    paths: Sequence[str],
) -> tuple[dict[str, list[str]], bool]:
    """同 find_torrents_by_path，另外返回「是否至少有一个下载器答上了话」。

    空结果有两种截然不同的含义：确实没有种子含这些文件，还是下载器全都挂了。
    调用方要据此决定是否把这批文件记成「查不到」—— 把下载器故障记成查不到，
    会让故障期间的所有关联白白攒够失败次数、进入降频期。
    """
    if not paths:
        return {}, True

    merged: dict[str, list[str]] = {}
    answered = False
    for name in list_configured_clients():
        client = get_download_client(name)
        if client is None:
            continue
        finder = getattr(client, "find_torrents_by_path", None)
        if finder is None:
            continue  # 该下载器未实现反查，跳过
        try:
            result = finder(paths)
        except Exception as exc:
            logger.warning(f"{name} 按路径反查种子异常: {exc}")
            continue
        answered = True
        for path, hashes in result.items():
            bucket = merged.setdefault(path, [])
            for h in hashes:
                if h not in bucket:
                    bucket.append(h)

    return merged, answered


def all_torrent_files_with_hashes() -> dict[str, list[str]]:
    """下载器里全部种子内文件 → 种子 hash 的完整映射，一趟拉完。

    存在的理由是省掉一次全量拉取。调用方（adopt_scrape_dir）原先要:

        _all_torrent_files()        取全部文件路径 —— 内部已经读到了 hash，却只返回路径
        find_torrents_by_path(...)  为了把 hash 找回来，再全量拉一遍

    两趟拉的是同一份数据，第二趟纯属重建第一趟刚扔掉的信息。种子上千、
    文件数万时每趟十几秒，白等一倍。

    各下载器的 find_torrents_by_path 本来就是「全量拉取建索引，再拿待查
    路径去命中」，唯一与路径有关的只有最后那道命中过滤。paths 传 None
    即表示不过滤、全都要，于是同一次拉取直接产出完整映射。
    """
    merged: dict[str, list[str]] = {}
    for name in list_configured_clients():
        client = get_download_client(name)
        if client is None:
            continue
        finder = getattr(client, "find_torrents_by_path", None)
        if finder is None:
            continue  # 该下载器未实现反查，跳过
        try:
            result = finder(None)
        except Exception as exc:
            logger.warning(f"{name} 读取全部种子文件映射异常: {exc}")
            continue
        for path, hashes in result.items():
            bucket = merged.setdefault(path, [])
            for h in hashes:
                if h not in bucket:
                    bucket.append(h)
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
