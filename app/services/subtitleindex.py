"""Directory-mtime incremental subtitle catalog, with daily reconciliation."""
import hashlib
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from app.database.models import SubtitleDirectory, SubtitleFile
from app.database.session import session_scope

_lock = threading.Lock()
_recent = {}  # At most 16 roots; no file contents/path dictionaries retained.


def identity(path):
    return hashlib.sha256(os.path.normcase(os.path.abspath(path)).encode()).hexdigest()


def refresh(directory, *, force=False):
    root = identity(directory)
    with _lock:
        now = time.monotonic()
        if not force and now - _recent.get(root, -60) < 60:
            return root
        directory = Path(directory)
        if not directory.is_dir() or directory.is_symlink():
            raise OSError('字幕目录当前不可用')
        from app.modules.subtitle.base import SUBTITLE_SUFFIXES
        from app.modules.subtitle.local import LocalSubtitle, _strip_version
        from app.utils import mediafile

        def walk(folder):
            stat = folder.stat()
            stamp = [stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns]
            key = identity(folder)
            with session_scope() as session:
                saved = session.get(SubtitleDirectory, key)
                unchanged = saved and saved.fingerprint == stamp and saved.checked_at > datetime.now()-timedelta(days=1)
                children = list(saved.children) if unchanged and not force else None
            if children is None:
                # Replace one directory in a transaction, using at most 200 file rows.
                # A failed enumeration rolls back to the complete previous snapshot.
                children, files = [], []
                with session_scope() as session:
                    saved = session.get(SubtitleDirectory, key)
                    session.execute(delete(SubtitleFile).where(SubtitleFile.directory == key))
                    with os.scandir(folder) as entries:
                        for entry in entries:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name not in ('@eaDir', '#recycle', '.unwanted'):
                                    children.append(entry.name)
                            elif Path(entry.name).suffix.lower() in SUBTITLE_SUFFIXES and entry.is_file(follow_symlinks=False):
                                path = Path(entry.path)
                                stem = re.sub(r'(?:[._-](?:zh[-_]?(?:cn|tw)|chs|cht))+$', '', path.stem, flags=re.I)
                                info = mediafile.parse(path.with_name(stem+path.suffix))
                                code = _strip_version(info.code or LocalSubtitle._code_of(path))
                                if code:
                                    files.append(dict(key=identity(path), root=root, directory=key, path=str(path), code=code, part=info.part))
                                    if len(files) >= 200:
                                        session.execute(SubtitleFile.__table__.insert(), files)
                                        files.clear()
                    if files:
                        session.execute(SubtitleFile.__table__.insert(), files)
                    current = folder.stat()
                    if [current.st_dev, current.st_ino, current.st_mtime_ns, current.st_ctime_ns] != stamp:
                        raise OSError('字幕目录扫描期间发生变化，保留旧索引')
                    removed = set(saved.children if saved else ()) - set(children)
                    for child in removed:
                        prefix = str(folder / child)
                        pattern = prefix.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + os.sep.replace('\\', '\\\\') + '%'
                        subdirs = select(SubtitleDirectory.key).where(SubtitleDirectory.root == root,
                            (SubtitleDirectory.path == prefix) | SubtitleDirectory.path.like(pattern, escape='\\'))
                        session.execute(delete(SubtitleFile).where(SubtitleFile.root == root, SubtitleFile.directory.in_(subdirs)))
                        session.execute(delete(SubtitleDirectory).where(SubtitleDirectory.key.in_(subdirs)))
                    if saved is None:
                        saved = SubtitleDirectory(key=key, root=root, path=str(folder))
                        session.add(saved)
                    saved.fingerprint, saved.children, saved.checked_at = stamp, children, datetime.now()
            for child in children:
                try:
                    walk(folder / child)
                except OSError:
                    # An unavailable subtree must not erase its last successful index.
                    continue
        walk(directory)
        if len(_recent) >= 16:
            _recent.clear()
        _recent[root] = time.monotonic()
        return root


def find(directory, code, part=0):
    from app.modules.subtitle.local import _strip_version
    root = refresh(directory)
    with session_scope() as session:
        paths = session.scalars(select(SubtitleFile.path).where(SubtitleFile.root == root,
            SubtitleFile.code == _strip_version(code), SubtitleFile.part == part).order_by(SubtitleFile.path).limit(20)).all()
    # Missing files are not evidence of an empty subtitle library.
    return next((Path(p) for p in paths if Path(p).is_file() and not Path(p).is_symlink()), None)
