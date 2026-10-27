"""Expire regenerable snapshots, never user history or recovery state."""
from datetime import datetime, timedelta
from sqlalchemy import delete
from app.database.models import Cache
from app.database.session import session_scope


def prune_snapshots(days=30):
    with session_scope() as session:
        result = session.execute(delete(Cache).where(Cache.namespace.in_(('rank', 'brand', 'media')),
            Cache.create_time < datetime.now()-timedelta(days=max(7, days))))
        return result.rowcount
