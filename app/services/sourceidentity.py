"""从已登记文件及下载历史恢复番号；不按演员名或公共下载目录猜测。"""
from __future__ import annotations

import os
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.database.models import History, MediaLink
from app.database.session import session_scope
from app.utils import get_true_code


def path_key(path: str | Path) -> str:
    # 只规范字符串，不 resolve/stat 网络磁盘，也不把 Linux 大小写不同的文件混同。
    return os.path.normcase(os.path.normpath(str(path)))


class SourceIdentityResolver:
    """每轮扫描复用一份快照；仅遇到缺号文件时读取下载器文件清单一次。"""
    def __init__(self):
        self._direct: dict[str, set[str]] | None = None
        self._history: dict[str, set[str]] = {}
        self._torrent_files: dict[str, set[str]] | None = None

    def _load(self):
        if self._direct is not None:
            return
        self._direct = {}
        with session_scope() as session:
            for source, link, code in session.execute(select(MediaLink.source_path, MediaLink.link_path, MediaLink.code)):
                normalized = get_true_code(code)
                if normalized:
                    for path in (source, link):
                        if path:
                            self._direct.setdefault(path_key(path), set()).add(normalized)
            for torrent_hash, code, saved in session.execute(select(History.hash, History.code, History.save_path)):
                normalized = get_true_code(code)
                if normalized:
                    self._history.setdefault(torrent_hash.lower(), set()).add(normalized)
                    # 旧记录可能保存文件路径；目录记录不能扩散到其下所有文件。
                    if saved:
                        self._direct.setdefault(path_key(saved), set()).add(normalized)

    def resolve(self, path: str | Path) -> str:
        try:
            self._load()
            direct = self._direct.get(path_key(path), set())
            if direct:
                return next(iter(direct)) if len(direct) == 1 else ''
            if not self._history:
                return ''
            if self._torrent_files is None:
                from app.modules.downloadclient import all_torrent_files_with_hashes
                from app.utils import mediafile
                self._torrent_files = {}
                files = all_torrent_files_with_hashes()
                by_hash: dict[str, list] = {}
                for filename, hashes in files.items():
                    if Path(filename).suffix.lower() not in ('.mp4', '.mkv', '.avi', '.wmv', '.mov', '.m4v', '.ts', '.iso'):
                        continue
                    info = mediafile.parse(filename)
                    if info.ignored or info.trailer:
                        continue
                    for torrent_hash in hashes:
                        by_hash.setdefault(torrent_hash.lower(), []).append(info)
                for torrent_hash, infos in by_hash.items():
                    codes = self._history.get(torrent_hash, set())
                    known = {i.code for i in infos if i.code}
                    # 合集种子不能把订阅的一部片编号套给其余所有无编号文件。
                    if len(infos) > 1 and (not known.issubset(codes) or not all(i.part for i in infos)):
                        continue
                    for info in infos:
                        self._torrent_files.setdefault(path_key(info.path), set()).update(codes)
            candidates = self._torrent_files.get(path_key(path), set())
            if len(candidates) == 1:
                code = next(iter(candidates))
                logger.info(f'[{code}] 通过下载器文件清单和种子历史恢复源文件番号')
                return code
            if candidates:
                logger.warning(f'[番号反查] 同一路径对应多个历史番号，保留待确认: {path}')
        except Exception:
            logger.warning('[番号反查] 下载历史或下载器暂不可用，保留待确认')
        return ''
