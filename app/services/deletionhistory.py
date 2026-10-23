"""删除之前先提交快照；删除后记录结果，重新订阅不读取此表作为去重凭据。"""
from contextlib import contextmanager
from datetime import datetime
import uuid

from sqlalchemy import select

from app.database.models import Code, DeletionRecord, History
from app.database.session import session_scope

_OWNER = uuid.uuid4().hex


def new_record(session, links, *, code='', source='cleanup', action='media_delete', status='running', plan=None):
    """调用方控制事务；仅删记录时与原记录删除共用一个事务。"""
    code = code or (links[0].code if links else '')
    item = session.get(Code, code) if code else None
    codes = {link.code for link in links} | {code}
    downloads = list(session.scalars(select(History).where(History.code.in_(codes))))
    row = DeletionRecord(id=uuid.uuid4().hex, code=code,
        title=(item.cn_title or item.title or '') if item else '', source=source, action=action,
        status=status, owner=_OWNER, link_path=links[0].link_path if links else '',
        source_path=links[0].source_path or '' if links else '',
        finished_at=datetime.utcnow() if status != 'running' else None,
        snapshot={'links': [link.to_dict() for link in links],
                  'downloads': [download.to_dict() for download in downloads],
                  'metadata': item.to_dict() if item else {}, 'plan': plan or {}}, result={}, error='')
    session.add(row)
    return row


@contextmanager
def record_deletion(links, result, *, source='cleanup', plan=None):
    # 快照落库失败即停止：不能先删除，再尝试留下唯一的追溯依据。
    with session_scope() as session:
        record = new_record(session, links, code=result.code, source=source, plan=plan)
        record_id = record.id
    error = ''
    try:
        yield record_id
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        with session_scope() as session:
            record = session.get(DeletionRecord, record_id)
            record.result = result.as_dict()
            record.error = error
            record.status = 'failed' if error else 'partial' if result.errors else 'completed'
            record.finished_at = datetime.utcnow()


def public_record(row, *, detail=False):
    data = row.to_dict()
    # 进程中断后不声称仍在执行，也不自动重放删除。
    if data['status'] == 'running' and row.owner != _OWNER:
        data['status'] = 'interrupted'
    for key in ('created_at', 'finished_at'):
        value = getattr(row, key)
        data[key] = value.isoformat() + 'Z' if value else None
    data.pop('owner', None)
    if not detail:
        data.pop('snapshot', None)
        data.pop('result', None)
    return data
