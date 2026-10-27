import copy
import os
import time
from decimal import Decimal

import pytest
from sqlalchemy import MetaData, create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.database.base import DBBase
from app.database.models import Code, ImageCacheFile
from app.services import cachemaintenance as cm
from app.utils import imagecache
from tests.test_resource_optimization import pictures, task
from tests.test_scrape_reliability import db


def test_decimal_checkpoint_stays_numeric_and_failure_is_persisted(pictures, monkeypatch):
    data = task('preview')
    def fail(state):
        state['total_bytes'] = Decimal('15006542432')
        raise RuntimeError('test failure after aggregate')
    monkeypatch.setattr(cm, '_scan', fail)
    cm._run(data)
    saved = cm.state()
    assert saved['phase'] == 'failed' and saved['running'] is False
    assert type(saved['total_bytes']) is int and saved['total_bytes'] == 15006542432
    assert 'RuntimeError' in saved['message']


def test_scanning_checkpoints_include_partial_bytes_without_claiming_total(pictures, monkeypatch):
    root, _, _ = pictures
    folder = root/'ABC-001'; folder.mkdir()
    for index in range(201):
        (folder/f'still_{index}.jpg').write_bytes(b'x'*1024)
    original = cm._save
    checkpoints = []
    def save(state, *args, **kwargs):
        original(state, *args, **kwargs)
        checkpoints.append(copy.deepcopy(state))
        if state.get('scanned') == 200:
            cm._stop.set()
    monkeypatch.setattr(cm, '_save', save)
    try:
        data = task('preview')
        assert cm._scan(data) is False
        assert checkpoints[-1]['scanned_bytes'] == 200*1024
        assert 'total_bytes' not in checkpoints[-1]
    finally:
        cm._stop.clear()


@pytest.fixture
def postgres_catalog(tmp_path, monkeypatch):
    url = os.getenv('CINEFOLD_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Set CINEFOLD_TEST_POSTGRES_URL to validate a PostgreSQL temporary catalog')
    engine = create_engine(url, connect_args={'connect_timeout': 10})
    with engine.connect() as connection:
        # Only session-local temporary tables; existing business tables stay untouched.
        connection.execute(text('SET search_path TO pg_temp'))
        metadata = MetaData()
        for table in DBBase.metadata.sorted_tables:
            copied = table.to_metadata(metadata)
            copied._prefixes = ['TEMPORARY']
        metadata.create_all(connection, checkfirst=False)
        factory = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False,
                               join_transaction_mode='create_savepoint')
        monkeypatch.setattr('app.database.session.SessionLocal', factory)
        root = tmp_path/'pics'; root.mkdir()
        monkeypatch.setattr(imagecache, 'PIC_DIR', root)
        monkeypatch.setattr(cm, 'get_settings', lambda: Settings())
        cm._stop.clear()
        try:
            yield root, factory
        finally:
            cm._stop.clear()
            connection.rollback()
    engine.dispose()


@pytest.mark.parametrize('empty', [False, True])
def test_postgres_full_scan_preview_saves_finished_state_and_exact_totals(postgres_catalog, empty):
    root, factory = postgres_catalog
    expected_bytes = 0
    if not empty:
        with factory.begin() as db:
            db.add_all([Code(code='ABC-001', status=0, banner='https://a/1.jpg'),
                        Code(code='ABC-002', status=1, banner='https://a/2.jpg')])
        for index in (1, 2):
            path = root/f'ABC-{index:03}'/'banner.jpg'
            path.parent.mkdir(); path.write_bytes(b'x'*(1024+index))
            expected_bytes += path.stat().st_size
            old = time.time()-40*86400; os.utime(path, (old, old))
    data = task('preview')
    cm._run(data)
    saved = cm.state()
    with factory() as db:
        aggregate = db.scalar(select(func.coalesce(func.sum(ImageCacheFile.size), 0)))
    assert isinstance(aggregate, Decimal), 'Reproduce PostgreSQL bigint aggregation, not SQLite behavior'
    assert saved['phase'] == 'finished' and saved['running'] is False
    assert saved['processed'] == (0 if empty else 2)
    assert type(saved['total_bytes']) is int and saved['total_bytes'] == expected_bytes
    assert saved['candidate_files'] == (0 if empty else 1)
    assert saved['candidate_bytes'] == (0 if empty else 1025)
    assert saved['changed'] == 0 and len(list(root.rglob('*.jpg'))) == (0 if empty else 2)
