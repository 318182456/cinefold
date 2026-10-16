"""健康检查与重复检索共用的稳定文件哈希；身份变化后缓存立即失效。"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from pathlib import Path

_LOCK = threading.Lock()
_CACHE = OrderedDict()
_LIMIT = 2048


def signature(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def identity(path, stat):
    return (stat.st_dev, stat.st_ino) if stat.st_ino else ('path', str(Path(path).resolve()))


def stable_digest(path, *, expected=None, cancelled=None, progress=None):
    path = Path(path)
    before = path.stat()
    stamp = signature(before)
    if expected is not None and stamp != signature(expected):
        raise ValueError('文件已经变化，请重新检查')
    key = (identity(path, before), stamp)
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError('已取消重复检索')
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return cached
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError('已取消重复检索')
            value.update(chunk)
            if progress is not None:
                progress(len(chunk))
    if signature(path.stat()) != stamp:
        raise ValueError('校验期间文件发生变化，请等待下载完成')
    with _LOCK:
        _CACHE[key] = value.hexdigest()
        _CACHE.move_to_end(key)
        while len(_CACHE) > _LIMIT:
            _CACHE.popitem(last=False)
    return value.hexdigest()
