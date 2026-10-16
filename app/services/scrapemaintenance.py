"""Coordinate library rebuilds with writers and preserve deletion protection."""
from __future__ import annotations

import json
import os
import inspect
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from app.database.session import DATA_DIR

_LOCK = threading.RLock()
_LOCAL = threading.local()
_ACTIVE = 0
_BUSY = False
_PROTECTION_FILE = DATA_DIR / 'scrape-rebuild-protection.json'


class MaintenanceBusy(ValueError):
    pass


def busy():
    with _LOCK:
        return _BUSY


@contextmanager
def activity():
    global _ACTIVE
    owned = bool(getattr(_LOCAL, 'internal', False))
    with _LOCK:
        if _BUSY and not owned:
            raise MaintenanceBusy('播放目录正在全量重建，请等待完成后重试')
        _ACTIVE += 1
    try:
        yield
    finally:
        with _LOCK:
            _ACTIVE -= 1


def guarded(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        with activity():
            return fn(*args, **kwargs)
    run.__signature__ = inspect.signature(fn, eval_str=True)
    return run


def reserve():
    global _BUSY
    with _LOCK:
        if _BUSY or _ACTIVE:
            raise ValueError('刮削、扫描或媒体库操作正在运行，请完成后再开始全量重建')
        _BUSY = True


def release():
    global _BUSY
    with _LOCK:
        _BUSY = False


@contextmanager
def internal():
    previous = getattr(_LOCAL, 'internal', False)
    _LOCAL.internal = True
    try:
        yield
    finally:
        _LOCAL.internal = previous


def protection():
    with _LOCK:
        try:
            roots = json.loads(_PROTECTION_FILE.read_text(encoding='utf-8'))
            if not isinstance(roots, list) or any(not isinstance(x, str) for x in roots):
                raise ValueError('invalid protection file')
            return {'deletion_protected': bool(roots), 'protected_outputs': roots}
        except FileNotFoundError:
            return {'deletion_protected': False, 'protected_outputs': []}
        except (OSError, ValueError):
            # A broken journal must never enable destructive webhook actions.
            return {'deletion_protected': True, 'protected_outputs': [],
                    'protection_error': '删除保护记录无法读取，请检查数据目录'}


def _save(roots):
    _PROTECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = _PROTECTION_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(roots, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, _PROTECTION_FILE)


def protect(output_dir):
    with _LOCK:
        state = protection()
        if state.get('protection_error'):
            raise ValueError(state['protection_error'])
        root = str(Path(output_dir).resolve())
        _save(sorted(set(state['protected_outputs']) | {root}))


def release_protection(confirmation):
    with _LOCK:
        if _BUSY or _ACTIVE:
            raise ValueError('媒体库任务仍在运行，暂不能解除删除保护')
        state = protection()
        if state.get('protection_error'):
            raise ValueError(state['protection_error'])
        if confirmation not in state['protected_outputs']:
            raise ValueError('请输入需要解除保护的完整输出目录')
        _save([x for x in state['protected_outputs'] if x != confirmation])
        return protection()


def protects_delete(link_path=''):
    state = protection()
    if not state['deletion_protected']:
        return False
    if not link_path or state.get('protection_error'):
        return True
    path = Path(link_path).resolve()
    return any(path == Path(root) or Path(root) in path.parents
               for root in state['protected_outputs'])
