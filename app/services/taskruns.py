"""调度任务共用执行入口：防重、有限并发和每项最近五次运行记录。"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from loguru import logger
from sqlalchemy import select

from app.database.models import Cache
from app.database.session import session_scope

_LOCK = threading.RLock()
_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix='manual-task')
_OWNER = uuid.uuid4().hex
_STATES = {}
_ACTIVE = set()
_LOADED = False


def _save(job_id):
    try:
        with session_scope() as session:
            row = session.scalar(select(Cache).where(Cache.namespace == 'scheduler-runs', Cache.key == job_id))
            if row is None:
                row = Cache(namespace='scheduler-runs', key=job_id)
                session.add(row)
            row.content = json.dumps(_STATES[job_id], ensure_ascii=False)
    except Exception:
        logger.exception(f'[任务:{job_id}] 运行记录保存失败，本次状态仅保留在内存')


def _load():
    global _LOADED
    if _LOADED:
        return
    with session_scope() as session:
        rows = [(row.key, row.content) for row in session.scalars(select(Cache).where(Cache.namespace == 'scheduler-runs'))]
    _LOADED = True
    for job_id, content in rows:
        try:
            state = json.loads(content or '{}')
            if not isinstance(state, dict) or not isinstance(state.get('history', []), list):
                raise ValueError('invalid task state')
            state['history'] = state.get('history', [])[-5:]
            _STATES[job_id] = state
            if state.get('phase') in ('queued', 'running') and state.get('owner') != _OWNER:
                state.update(phase='interrupted', finished_at=time.time(), error='服务重启，上次执行已中断；请核对日志后重新执行')
                state['history'] = [*state['history'], {k: v for k, v in state.items() if k not in ('history', 'owner')}][-5:]
                _save(job_id)
        except (ValueError, TypeError):
            logger.warning(f'[任务:{job_id}] 运行记录损坏，未将其视为执行成功')


def states():
    with _LOCK:
        _load()
        return copy.deepcopy(_STATES)


def progress(job_id, message, *, current='', processed=0):
    """Publish bounded progress for long scheduler tasks without keeping payloads."""
    with _LOCK:
        state=_STATES.get(job_id)
        if not state or state.get('phase')!='running':return
        state['progress']={'message':message,'current':current,'processed':int(processed),'updated_at':time.time()}
        _save(job_id)


def _reserve(job_id, source):
    with _LOCK:
        _load()
        if job_id in _ACTIVE:
            raise ValueError('该任务已在排队或执行中，请勿重复触发')
        if source == 'manual' and sum(state.get('phase') == 'queued' for state in _STATES.values()) >= 20:
            raise ValueError('手动任务队列已满，请等待现有任务完成')
        state = {'job_id': job_id, 'run_id': uuid.uuid4().hex, 'owner': _OWNER,
                 'phase': 'queued', 'source': source, 'queued_at': time.time(), 'started_at': None,
                 'finished_at': None, 'duration': None, 'processed': None, 'error': '',
                 'history': _STATES.get(job_id, {}).get('history', [])[-5:]}
        _ACTIVE.add(job_id)
        _STATES[job_id] = state
        _save(job_id)
        return state['run_id']


def _finish(job_id, run_id, phase, *, result=None, error=''):
    with _LOCK:
        state = _STATES[job_id]
        if state['run_id'] != run_id:
            return
        ended = time.time()
        state.update(phase=phase, finished_at=ended, error=error,
                     duration=round(max(0, ended - (state['started_at'] or state['queued_at'])), 2),
                     processed=result if type(result) is int else None)
        state['history'] = [*state['history'], {k: v for k, v in state.items() if k not in ('history', 'owner')}][-5:]
        _ACTIVE.discard(job_id)
        _save(job_id)


def _execute(job_id, func, run_id):
    # Playback polling remains responsive while scans/imports wait in queued state.
    from contextlib import nullcontext
    from app.services.resourcebudget import slot
    with (nullcontext() if job_id in ('poll_playback', 'sync_download_history', 'pt_wait') else slot()):
        return _execute_reserved(job_id, func, run_id)


def _execute_reserved(job_id, func, run_id):
    from app.services.memoryusage import snapshot
    with _LOCK:
        state = _STATES[job_id]
        state.update(phase='running', started_at=time.time(), process_memory_before=snapshot())
        _save(job_id)
    logger.info(f'[任务:{job_id}] 开始执行，来源={state["source"]}')
    try:
        result = func()
    except BaseException as exc:
        with _LOCK:
            state['process_memory_after'] = snapshot()
        message = re.sub(r'(?i)\b[a-z][a-z0-9+.-]*://[^\s<>"\']+', '[地址已隐藏]', str(exc))
        message = re.sub(r'(?i)\b(passkey|token|api[_-]?key|password)\s*[=:]\s*[^\s&,;]+', r'\1=[已隐藏]', message)
        _finish(job_id, run_id, 'failed', error=f'{type(exc).__name__}: {message}'[:500])
        logger.exception(f'[任务:{job_id}] 执行失败')
        raise
    else:
        with _LOCK:
            state['process_memory_after'] = snapshot()
        _finish(job_id, run_id, 'finished', result=result)
        logger.info(f'[任务:{job_id}] 执行结束，返回数量={result if type(result) is int else "未提供"}')
        return result


def submit(job_id, func):
    """手动调用在提交线程池前占位，同一任务不会排入多个副本。"""
    run_id = _reserve(job_id, 'manual')
    try:
        _POOL.submit(_execute, job_id, func, run_id)
    except Exception:
        _finish(job_id, run_id, 'failed', error='后台任务提交失败，请重试')
        raise


def scheduled(job_id, func):
    """定时调用与手动调用共享运行所有权。"""
    try:
        run_id = _reserve(job_id, 'scheduled')
    except ValueError as exc:
        logger.info(f'[任务:{job_id}] 本次定时触发已跳过：{exc}')
        return None
    return _execute(job_id, func, run_id)
