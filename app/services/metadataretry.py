"""Durable admission and backoff for automatic metadata enrichment.

The existing Cache table stores the next eligible time in create_time for this
namespace only. No Redis dependency: restarting must not reset failed lookups.
Manual searches and scrape/retry jobs deliberately do not use this admission.
"""
from datetime import datetime, timedelta
from contextlib import nullcontext
import json

from sqlalchemy import delete, exists
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database.models import Cache
from app.database.session import session_scope

NAMESPACE = "metadata-retry"
EMPTY_SECONDS = 6 * 3600
ERROR_SECONDS = 10 * 60
LEASE_SECONDS = 5 * 60  # Longer than get_code_detail's 120-second network budget.


def eligible(column, now=None, *, namespace=NAMESPACE):
    """Exclude cooling/in-flight entries before applying a batch's LIMIT."""
    return ~exists().where(
        Cache.namespace == namespace,
        Cache.key == column,
        Cache.create_time > (now or datetime.now()),
    )


def _statement(session, code, until, reason, namespace=NAMESPACE):
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    statement = insert(Cache).values(
        namespace=namespace, key=code, create_time=until,
        content=json.dumps({"reason": reason, "retry_at": until.isoformat()}),
    )
    return statement


def claim(code, *, namespace=NAMESPACE, lease_seconds=LEASE_SECONDS):
    """An atomic expiring lease also deduplicates concurrent processes/tasks."""
    now = datetime.now()
    with session_scope() as session:
        statement = _statement(session, code, now + timedelta(seconds=lease_seconds), "inflight", namespace)
        statement = statement.on_conflict_do_update(
            index_elements=[Cache.namespace, Cache.key],
            set_={"content": statement.excluded.content, "create_time": statement.excluded.create_time},
            where=Cache.create_time <= now,
        ).returning(Cache.id)
        return session.scalar(statement) is not None


def defer(code, retryable=True, session=None, *, namespace=NAMESPACE):
    seconds = ERROR_SECONDS if retryable else EMPTY_SECONDS
    until = datetime.now() + timedelta(seconds=seconds)
    with (nullcontext(session) if session is not None else session_scope()) as session:
        statement = _statement(session, code, until, "error" if retryable else "empty", namespace)
        session.execute(statement.on_conflict_do_update(
            index_elements=[Cache.namespace, Cache.key],
            set_={"content": statement.excluded.content, "create_time": statement.excluded.create_time},
        ))
    return until


def clear(session, code, *, namespace=NAMESPACE):
    """Release only in the same transaction that saves the successful details."""
    session.execute(delete(Cache).where(Cache.namespace == namespace, Cache.key == code))
