"""Persist image validation by file identity; unchanged artwork needs only stat()."""
from __future__ import annotations

import hashlib
import json
import time
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database.models import Cache
from app.database.session import session_scope
from app.modules.scrape import images

NAMESPACE = 'artifact-image-v1'
_db_retry_at = 0.0


@lru_cache(maxsize=8192)
def _validated(path: str, fingerprint: tuple) -> bool:
    global _db_retry_at
    key = hashlib.sha256(path.encode()).hexdigest()
    if time.monotonic() >= _db_retry_at:
        try:
            with session_scope() as session:
                saved = session.scalar(select(Cache.content).where(Cache.namespace == NAMESPACE, Cache.key == key))
            data = json.loads(saved) if saved else {}
            if data.get('fingerprint') == list(fingerprint) and data.get('valid') is True:
                try:
                    if images._fingerprint(Path(path).stat()) == fingerprint:
                        return True
                    raise images.InvalidImageError()
                except OSError:
                    raise images.InvalidImageError() from None
        except images.InvalidImageError:
            raise
        except Exception:
            # Cache outages must not prevent local validation or make every
            # following image repeat the same failed database connection.
            _db_retry_at = time.monotonic() + 60
    valid = images._decode_file(path, fingerprint)
    if not valid:
        raise images.InvalidImageError()
    try:
        if images._fingerprint(Path(path).stat()) != fingerprint:
            raise images.InvalidImageError()
    except OSError:
        raise images.InvalidImageError() from None
    if time.monotonic() >= _db_retry_at:
        try:
            with session_scope() as session:
                stmt = (pg_insert if session.bind.dialect.name == 'postgresql' else sqlite_insert)(Cache)
                content = json.dumps({'fingerprint': fingerprint, 'valid': valid})
                session.execute(stmt.values(namespace=NAMESPACE, key=key, content=content).on_conflict_do_update(
                    index_elements=['namespace', 'key'], set_={'content': content}))
        except Exception:
            _db_retry_at = time.monotonic() + 60
    return valid


def valid_image(path) -> bool:
    if images.Image is None:
        return False
    try:
        path = Path(path)
        return _validated(str(path), images._fingerprint(path.stat()))
    except (OSError, images.InvalidImageError):
        return False
