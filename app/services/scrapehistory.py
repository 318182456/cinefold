"""Task history never removes media, downloads, or quarantine backups."""
from datetime import datetime
from functools import wraps
import json
from pathlib import Path
import time
import threading
import uuid

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.database.models import ScrapeTask, ScrapeTaskResult
from app.database.session import session_scope
from app.utils import get_true_code

_ACTIVE = {'running', 'checking', 'quarantine', 'backup', 'clearing', 'rebuilding'}
_INDEX_LOCK = threading.Lock()
_AUTOMATIC_LOCK = threading.Lock()
_AUTOMATIC = set()


class AutomaticTask:
    """Persist automatic batches through the same history contract as manual tasks."""
    def __init__(self, groups, settings, trigger):
        self.groups = groups
        self.job_id = uuid.uuid4().hex
        self.options = dict(overwrite=settings.scrape_overwrite, refresh_metadata=False,
                            fetch_meta=True, force=False, trigger=trigger)
        self.state = dict(job_id=self.job_id, phase='running', total=len(groups), done=0, ok=0)

    def __enter__(self):
        with _AUTOMATIC_LOCK:
            _AUTOMATIC.add(self.job_id)
        try:
            begin(self.job_id, 'automatic', [dict(path=str(item.path), code=code)
                for code, items in self.groups.items() for item in items], self.options, len(self.groups))
            return self
        except BaseException:
            with _AUTOMATIC_LOCK:
                _AUTOMATIC.discard(self.job_id)
            raise

    def process(self, code, items, **kwargs):
        from app.services import scrape, scrapesources
        scrapesources.record_attempt(items, code, status='running', job_id=self.job_id)
        try:
            result = scrape.scrape_group(code, items, **kwargs)
        except Exception as exc:
            logger.exception(f'[{code}] 自动刮削失败')
            result = scrape.ScrapeResult(code=code, error=str(exc) or type(exc).__name__)
        scrapesources.record_attempt(items, code, status='succeeded' if result.ok else 'failed',
                                    job_id=self.job_id, error=result.error)
        record(self.job_id, dict(code=code, ok=result.ok, paths=[str(item.path) for item in items],
            links=[str(path) for path in result.links], error=result.error, warnings=result.warnings,
            sources=result.sources, nfo_written=result.nfo_written, images_written=result.images_written))
        self.state['done'] += 1
        self.state['ok'] += int(result.ok)
        sync(self.state, 'automatic')
        return result

    def __exit__(self, exc_type, exc, traceback):
        try:
            phase = 'failed' if exc_type else 'completed' if self.state['done']==self.state['ok']==self.state['total'] else 'partial'
            sync({**self.state, 'phase':phase, 'finished_at':time.time(),
                  'error':str(exc) if exc else ''}, 'automatic')
        finally:
            with _AUTOMATIC_LOCK:
                _AUTOMATIC.discard(self.job_id)


def _best_effort(function):
    @wraps(function)
    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except SQLAlchemyError:
            logger.exception('保存刮削历史失败，影片任务继续；重建任务仍可按恢复清单核对')
    return run


@_best_effort
def begin(job_id, kind, sources, options, total, parent_id=''):
    with session_scope() as session:
        row = session.get(ScrapeTask, job_id)
        if row is None:
            row = ScrapeTask(job_id=job_id, kind=kind, started_at=time.time())
            session.add(row)
        row.sources, row.options, row.total = sources, options, total
        row.parent_id = parent_id or row.parent_id or ''


@_best_effort
def sync(state, kind='rebuild'):
    job_id = state.get('job_id')
    if not job_id:
        return
    with session_scope() as session:
        row = session.get(ScrapeTask, job_id)
        if row is None:
            row = ScrapeTask(job_id=job_id, kind=kind, started_at=state.get('started_at') or time.time())
            session.add(row)
        for key in ('phase', 'total', 'done', 'ok', 'error', 'quarantine_dir', 'finished_at'):
            if key in state and (state[key] is not None or key == 'finished_at'):
                setattr(row, key, state[key])


@_best_effort
def record(job_id, result):
    code = get_true_code(result['code']) or result['code']
    with session_scope() as session:
        session.merge(ScrapeTaskResult(job_id=job_id, code=code, ok=bool(result['ok']), data={**result, 'code':code}))


def hidden(job_id):
    if not job_id:
        return False
    with session_scope() as session:
        row = session.get(ScrapeTask, job_id)
        return bool(row and row.hidden)


def hidden_ids():
    with session_scope() as session:
        return set(session.scalars(select(ScrapeTask.job_id).where(ScrapeTask.hidden.is_(True))))


def _live():
    from app.services import scraperebuild, scrapesources
    rebuild = scraperebuild.state()
    manual = scrapesources.state()
    with _AUTOMATIC_LOCK:
        automatic = set(_AUTOMATIC)
    return automatic | {row.get('job_id') or '__preview__' for row in (rebuild, manual) if row.get('running')}


def _legacy():
    from app.services import scraperebuild, scraperecovery
    current = scraperebuild.state()
    try:
        history = scraperecovery.history(scraperebuild)
    except (ValueError, OSError):
        history = []  # Manual history remains usable before output configuration.
    for item in history:
        with session_scope() as session:
            row = session.get(ScrapeTask, item['job_id'])
            if row is None:
                row = ScrapeTask(job_id=item['job_id'], kind='rebuild',
                    started_at=datetime.fromisoformat(item['created_at']).timestamp() if item.get('created_at') else time.time())
                session.add(row)
            row.quarantine_dir = item['quarantine_dir']
            if not row.indexed:
                row.phase = item.get('phase') or 'interrupted'
                row.total, row.done, row.ok = item.get('total') or 0, item.get('done') or 0, item.get('ok') or 0
            if item['job_id'] == current.get('job_id') and current['phase'] not in ('idle','preview','ready','previewing','planning'):
                for key in ('phase','total','done','ok','error','finished_at'):
                    if current.get(key) is not None:
                        setattr(row, key, current[key])


def _plain(row, live):
    running = row.job_id in live
    phase = 'interrupted' if row.phase in _ACTIVE and not running else row.phase
    return {key:getattr(row,key) for key in ('job_id','kind','parent_id','total','done','ok','error',
            'quarantine_dir','started_at','finished_at')} | {
        'phase':phase, 'running':running, 'failed':max(0,row.done-row.ok), 'pending':max(0,row.total-row.done),
        'can_resume':not running and phase != 'completed' and (row.kind in ('manual','automatic') or bool(row.quarantine_dir)),
        'can_retry_failed':not running and row.done>row.ok and (row.kind in ('manual','automatic') or bool(row.quarantine_dir))}


def listing(page=1, size=20):
    _legacy()
    live = _live()
    with session_scope() as session:
        query = select(ScrapeTask).where(ScrapeTask.hidden.is_(False))
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        rows = session.scalars(query.order_by(ScrapeTask.started_at.desc()).offset((page-1)*size).limit(size)).all()
        return {'items':[_plain(row,live) for row in rows], 'total':total, 'page':page, 'size':size, 'running':bool(live)}


def _index(job_id):
    with _INDEX_LOCK:
        _index_legacy(job_id)


def _index_legacy(job_id):
    """Import a legacy manifest once, only after the user opens its details."""
    from app.services import scraperebuild, scraperecovery
    with session_scope() as session:
        task = session.get(ScrapeTask, job_id)
        if task is None or task.hidden:
            raise ValueError('刮削记录不存在或已删除')
        if task.indexed or task.kind != 'rebuild' or not task.quarantine_dir or job_id in _live():
            return
        directory = task.quarantine_dir
    candidate = next((row for row in scraperecovery.history(scraperebuild) if row['job_id']==job_id), None)
    if not candidate or candidate['quarantine_dir'] != directory:
        raise ValueError('原任务备份目录不可用，记录仍保留')
    path = Path(directory) / 'manifest.json'
    if scraperebuild._unsafe_links(path):
        raise ValueError('恢复清单路径不安全')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('job_id') != job_id:
        raise ValueError('恢复清单与任务标识不一致')
    latest = {get_true_code(row['code']) or row['code']:row for row in manifest.get('results',[])}
    journal = Path(directory)/'results.jsonl'
    if scraperebuild._unsafe_links(journal):
        raise ValueError('任务结果日志路径不安全')
    if journal.is_file():
        lines = journal.read_bytes().splitlines(keepends=True)
        for index,line in enumerate(lines):
            try:
                row = json.loads(line)
                latest[get_true_code(row['code']) or row['code']] = row
            except (ValueError,KeyError,TypeError):
                if index==len(lines)-1 and not line.endswith(b'\n'):
                    break
                raise ValueError('结果日志损坏，未删除任何记录或文件')
    with session_scope() as session:
        task = session.get(ScrapeTask,job_id)
        if task.hidden:
            raise ValueError('刮削记录已删除')
        task.sources = [{'path':row['path'],'code':get_true_code(row['code']) or row['code']} for row in manifest.get('sources',[])]
        task.options = manifest.get('options',{})
        for code,row in latest.items():
            if session.get(ScrapeTaskResult,(job_id,code)) is None:
                session.add(ScrapeTaskResult(job_id=job_id,code=code,ok=bool(row['ok']),data={**row,'code':code}))
        session.flush()
        results = list(session.scalars(select(ScrapeTaskResult).where(ScrapeTaskResult.job_id==job_id)))
        task.total = len({row['code'] for row in task.sources})
        task.done = len(results)
        task.ok = sum(row.ok for row in results)
        task.indexed = True


def detail(job_id, page=1, size=30, status=''):
    _index(job_id)
    live = _live()
    with session_scope() as session:
        task = session.get(ScrapeTask,job_id)
        if task is None or task.hidden:
            raise ValueError('刮削记录不存在或已删除')
        query = select(ScrapeTaskResult).where(ScrapeTaskResult.job_id==job_id)
        if status:
            query = query.where(ScrapeTaskResult.ok.is_(status=='succeeded'))
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        rows = session.scalars(query.order_by(ScrapeTaskResult.code).offset((page-1)*size).limit(size)).all()
        return {'task':_plain(task,live), 'items':[row.data for row in rows], 'total':total, 'page':page, 'size':size}


def resume(job_id, mode='unfinished'):
    from app.services import scraperecovery, scrapesources
    if mode not in ('unfinished', 'failed'):
        raise ValueError('不支持的恢复方式')
    _index(job_id)
    if _live():
        raise ValueError('已有刮削或检查任务运行中，请等待完成')
    with session_scope() as session:
        task = session.get(ScrapeTask,job_id)
        if task is None or task.hidden:
            raise ValueError('刮削记录不存在或已删除')
        kind, options = task.kind, dict(task.options or {})
        rows = {row.code:row.data for row in session.scalars(select(ScrapeTaskResult).where(ScrapeTaskResult.job_id==job_id))}
        sources = [row for row in task.sources if ((get_true_code(row['code']) or row['code']) in rows and
            not rows[get_true_code(row['code']) or row['code']]['ok']) or
            mode=='unfinished' and (get_true_code(row['code']) or row['code']) not in rows]
    if kind == 'rebuild':
        return scraperecovery.start(job_id,mode)
    if not sources:
        raise ValueError('没有符合条件的未完成或失败影片')
    return scrapesources.start(sources, **{key:options[key] for key in ('overwrite','refresh_metadata','fetch_meta','force') if key in options}, parent_job_id=job_id)


def remove(job_id):
    from app.services import scraperebuild, scrapesources
    with scraperebuild._LOCK, scrapesources._LOCK:
        if job_id in _live():
            raise ValueError('任务运行中，不能删除记录')
        with session_scope() as session:
            task = session.get(ScrapeTask,job_id)
            if task is None or task.hidden:
                raise ValueError('刮削记录不存在或已删除')
            task.hidden = True
        scraperebuild.dismiss(job_id)
        if not scrapesources._STATE['running'] and scrapesources._STATE.get('job_id')==job_id:
            scrapesources._STATE.update(job_id='',done=0,total=0,ok=0,current='',results=[],error='',cancel_requested=False)
    return {'deleted':True, 'files_deleted':False}
