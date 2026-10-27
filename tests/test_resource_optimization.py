import io
import os
import random
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import Settings
from app.database.models import Cache, Code, DownloadRecord, History, ImageCacheFile
from app.services import cachemaintenance, downloads, storagepolicy, subtitleindex
from app.utils import imagecache
from tests.test_scrape_sources import env, video
from tests.test_scrape_reliability import db


def jpeg():
    rng = random.Random(46)
    pixels = bytes(rng.randrange(256) for _ in range(320*240*3))
    with Image.frombytes('RGB', (320, 240), pixels) as image:
        out = io.BytesIO()
        image.save(out, format='JPEG', quality=97)
        return out.getvalue()


@pytest.fixture
def pictures(tmp_path, monkeypatch, db):
    root = tmp_path/'pics'
    root.mkdir()
    settings = Settings()
    monkeypatch.setattr(imagecache, 'PIC_DIR', root)
    monkeypatch.setattr('app.core.config.get_settings', lambda: settings)
    monkeypatch.setattr(cachemaintenance, 'get_settings', lambda: settings)
    cachemaintenance._stop.clear()
    return root, settings, db


def task(mode):
    return dict(mode=mode, policy=cachemaintenance._policy(), stage='scan', cursor='', scanned=0, processed=0,
        changed=0, saved_bytes=0, candidate_files=0, candidate_bytes=0, errors=[], started_at=time.time())


def test_webp_migration_old_url_and_different_stills(pictures):
    root, _, _ = pictures
    old = root/'ABC-001'/'banner.jpg'
    old.parent.mkdir(); old.write_bytes(jpeg())
    data = task('optimize')
    cachemaintenance._run(data)
    assert data['phase'] == 'finished' and data['changed'] == 1 and data['saved_bytes'] > 0
    resolved = imagecache.resolve_relative('ABC-001/banner.jpg')
    assert resolved.suffix == '.webp' and not old.exists()
    with Image.open(resolved) as image:
        assert image.format == 'WEBP' and image.size == (320, 240)
    first = imagecache.cache_path('https://a/1.jpg', 'ABC-001', 'still')
    second = imagecache.cache_path('https://a/2.jpg', 'ABC-001', 'still')
    assert first != second
    assert imagecache.cache_path('https://a/1.jpg', 'ABC-001', '../outside') is None


def test_preview_and_clean_protect_important_and_unknown_images(pictures):
    root, settings, factory = pictures
    with factory.begin() as db:
        db.add_all([Code(code='ABC-001', status=1, banner='https://a/1.jpg'),
                    Code(code='ABC-002', status=0, banner='https://a/2.jpg')])
    for code, name in [('ABC-001', 'banner.jpg'), ('ABC-001', 'still_abc.jpg'), ('ABC-002', 'banner.jpg'), ('UNKNOWN', 'banner.jpg')]:
        path = root/code/name; path.parent.mkdir(exist_ok=True); path.write_bytes(jpeg())
        old = time.time()-86400*40; os.utime(path, (old, old))
    preview = task('preview'); cachemaintenance._run(preview)
    assert preview['candidate_files'] == 2 and preview['changed'] == 0
    assert len(list(root.rglob('*.jpg'))) == 4
    clean = task('clean'); cachemaintenance._run(clean)
    assert clean['changed'] == 2
    assert (root/'ABC-001'/'banner.jpg').exists() and (root/'UNKNOWN'/'banner.jpg').exists()
    assert not (root/'ABC-002'/'banner.jpg').exists() and not (root/'ABC-001'/'still_abc.jpg').exists()


def test_new_access_after_preview_prevents_deletion(pictures):
    root, _, factory = pictures
    with factory.begin() as db: db.add(Code(code='ABC-001', banner='https://a/1.jpg'))
    path = root/'ABC-001'/'banner.jpg'; path.parent.mkdir(); path.write_bytes(jpeg())
    old = time.time()-86400*40; os.utime(path, (old, old))
    data = task('clean'); cachemaintenance._scan(data)
    imagecache.touch(path)
    cachemaintenance._process(data)
    assert path.exists() and data['changed'] == 0


def test_image_maintenance_pause_resume_does_not_reencode_completed(pictures, monkeypatch):
    root, _, _ = pictures
    for code in ['ABC-001', 'ABC-002']:
        path = root/code/'banner.jpg'; path.parent.mkdir(); path.write_bytes(jpeg())
    write = imagecache.atomic_write
    writes = []
    def pause_after_one(path, content):
        write(path, content); writes.append(str(path)); cachemaintenance._stop.set()
    monkeypatch.setattr(imagecache, 'atomic_write', pause_after_one)
    data = task('optimize'); cachemaintenance._run(data)
    assert data['phase'] == 'paused' and data['changed'] == 1
    completed = Path(writes[0]); fingerprint = cachemaintenance._fingerprint(completed)
    monkeypatch.setattr(imagecache, 'atomic_write', write)
    cachemaintenance._stop.clear(); cachemaintenance._run(data)
    assert data['phase'] == 'finished' and data['changed'] == 2
    assert cachemaintenance._fingerprint(completed) == fingerprint


def test_bad_image_and_animation_are_preserved(pictures):
    bad = b'not-a-real-image'*100
    assert imagecache.optimized(bad) == (bad, None)
    buf = io.BytesIO()
    with Image.new('RGB', (100, 100), 'red') as a, Image.new('RGB', (100, 100), 'blue') as b:
        a.save(buf, format='GIF', save_all=True, append_images=[b])
    assert imagecache.optimized(buf.getvalue()) == (buf.getvalue(), None)


def test_proxy_returns_converted_bytes_with_matching_mime_and_etag(pictures, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.endpoints import picproxy
    from app.api.endpoints import get_current_user
    app = FastAPI(); app.include_router(picproxy.router)
    app.dependency_overrides[get_current_user] = lambda:'test'
    source = jpeg()
    remote = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,content=source,headers={'content-type':'image/jpeg'})))
    monkeypatch.setattr(picproxy, '_get_client', lambda:remote)
    with TestClient(app) as client:
        response = client.get('/image-proxy',params={'url':'https://javdb.com/test.jpg','code':'ABC-001'})
        target = imagecache.find_cached('https://javdb.com/test.jpg','ABC-001')
        assert response.status_code == 200 and response.headers['content-type'] == 'image/webp'
        assert response.content == target.read_bytes()
        assert response.headers['etag'] == imagecache.etag_for(target)
        assert client.get('/image-local',params={'path':'ABC-001/banner.jpg'},headers={'if-none-match':response.headers['etag']}).status_code == 304
        thumb = client.get('/image-local',params={'path':'ABC-001/banner.jpg','thumb':True})
        with Image.open(io.BytesIO(thumb.content)) as image:
            assert max(image.size) <= 480 and image.format == 'WEBP'
        target.unlink()
        with pictures[2].begin() as db: db.add(Code(code='ABC-001',banner='https://javdb.com/test.jpg'))
        fallback = client.get('/image-local',params={'path':'ABC-001/banner.jpg'},follow_redirects=False)
        assert fallback.status_code == 307 and fallback.headers['location'].startswith('/api/v1/image-proxy?')


def test_transparent_png_keeps_alpha_and_corrupt_url_suffix_is_corrected(pictures):
    buf = io.BytesIO()
    rng = random.Random(3)
    with Image.frombytes('RGBA',(100,100),bytes(rng.randrange(256) for _ in range(100*100*4))) as image:
        image.save(buf,format='PNG')
    content = buf.getvalue()
    data, suffix = imagecache.optimized(content)
    assert suffix == '.webp'
    with Image.open(io.BytesIO(data)) as image:
        assert 'A' in image.getbands()
    settings = pictures[1]; settings.image_cache_webp = False
    path = imagecache.store(data,'https://a/bad.jpg','ABC-001')
    assert path.suffix == '.webp'


def test_image_maintenance_real_restart_reads_checkpoint(pictures, monkeypatch):
    import json
    from app.services import cachemaintenance as cm
    old = task('clean'); old.update(running=True, phase='running', stage='process', cursor='abc', cursor_access=3., processed=25, changed=4)
    cm._save(old)
    assert cm.state()['phase'] == 'interrupted'
    captured = []
    class Deferred:
        def __init__(self, *, target, args, **kwargs): captured.append(args[0])
        def start(self): pass
    monkeypatch.setattr(cm.threading,'Thread',Deferred)
    try:
        resumed = cm.start(resume=True)
        assert resumed['cursor'] == 'abc' and captured[0]['processed'] == 25 and captured[0]['mode'] == 'clean'
    finally:
        cm._running = False


def test_completed_tasks_reuse_metadata_but_failures_force_full_retry(db, monkeypatch):
    settings = Settings(qbittorrent_url='http://qb')
    monkeypatch.setattr(downloads,'get_settings',lambda:settings)
    monkeypatch.setattr(downloads,'list_configured_clients',lambda:['qbittorrent'])
    monkeypatch.setattr(downloads,'_LIVE',None)
    reads=[]
    def read(name): reads.append(name); return [{'hash':'a'*40,'progress':1,'state':'seeding','name':'completed'}]
    monkeypatch.setattr(downloads,'_read',read)
    downloads.live()
    remembered=[]
    monkeypatch.setattr(downloads,'_remember',lambda rows:remembered.append(len(rows)))
    monkeypatch.setattr(downloads,'get_download_client',lambda _:SimpleNamespace())
    downloads._LIVE['updated_at']=0
    assert downloads.live()['total']==1
    assert remembered == [0] and reads == ['qbittorrent']
    downloads._LIVE['errors']=[{'client':'qbittorrent'}]; downloads._LIVE['updated_at']=0
    downloads.live()
    assert reads == ['qbittorrent','qbittorrent']


def test_subtitle_index_reuses_db_after_instance_restart_and_updates_changes(db, tmp_path, monkeypatch):
    from app.modules.subtitle.local import LocalSubtitle
    root = tmp_path/'subs'; (root/'nested').mkdir(parents=True)
    (root/'ABC-001.srt').write_text('placeholder')
    first = subtitleindex.find(root, 'ABC-001-UC')
    assert first == root/'ABC-001.srt'
    subtitleindex._recent.clear()
    scan = subtitleindex.os.scandir
    calls = []
    monkeypatch.setattr(subtitleindex.os, 'scandir', lambda *a, **k: (calls.append(a) or scan(*a, **k)))
    assert subtitleindex.find(root, 'ABC-001') == first and calls == []
    (root/'nested'/'ABC-002.srt').write_text('placeholder')
    subtitleindex._recent.clear()
    assert subtitleindex.find(root, 'ABC-002') == root/'nested'/'ABC-002.srt'
    assert len(calls) == 1, 'Only the changed directory should be enumerated'
    (root/'nested'/'ABC-002.srt').unlink(); (root/'nested').rmdir()
    subtitleindex._recent.clear()
    assert subtitleindex.find(root, 'ABC-002') is None


def test_snapshot_expiration_preserves_history_and_checkpoints(db):
    old = datetime.now()-timedelta(days=60)
    with db.begin() as session:
        session.add_all([Cache(namespace=ns, key='saved', content='{}', create_time=old)
            for ns in ['rank', 'brand', 'media', 'scheduler-runs', 'download-transfer', 'metadata-retry', 'image-maintenance']])
        session.add(History(hash='a'*40, code='ABC-001'))
    assert storagepolicy.prune_snapshots() == 3
    with db() as session:
        assert session.get(History, 'a'*40)
        assert set(session.scalars(select(Cache.namespace))) == {'scheduler-runs','download-transfer','metadata-retry','image-maintenance'}


def test_incremental_download_refresh_only_queries_active_hashes(monkeypatch):
    calls = []
    client = SimpleNamespace(list_downloads=lambda **kw: (calls.append(kw) or [dict(hash='a', progress=.9, state='downloading')]))
    monkeypatch.setattr(downloads, 'get_download_client', lambda _: client)
    previous = [dict(hash='a', progress=.5, state='downloading'), dict(hash='b', progress=1, state='seeding')]
    rows = downloads._read_incremental('qbittorrent', previous)
    assert calls == [{'hashes': ['a']}]
    assert {row['hash']:row['progress'] for row in rows} == {'a': .9, 'b': 1}


def test_history_sql_pagination_filter_and_legacy_dedup(db):
    with db.begin() as session:
        session.add_all([Code(code='ABC-001', status=2),
            History(hash='A'*40, code='ABC-001'), History(hash='b'*40, code='ABC-001'),
            DownloadRecord(key='one', hash='a'*40, code='ABC-001', client='qbittorrent', details={'name':'test_100%', 'code':'ABC-001'})])
    assert downloads.history()['total'] == 2
    result = downloads.history(keyword='_100%', code_status=2)
    assert result['total'] == 1 and result['items'][0]['name'] == 'test_100%'
    assert len(downloads.history(size=1, page=2)['items']) == 1
    assert downloads.history(client='transmission')['total'] == 0


def test_inventory_incremental_reuses_unchanged_then_detects_replacement(env, monkeypatch):
    from app.services import scrapeinventory
    from app.services import scrape
    root, _, factory, *_ = env
    path = video(root)
    calls = []
    inspect = scrape.inspect_source
    monkeypatch.setattr(scrape, 'inspect_source', lambda *args: (calls.append(args[0]) or inspect(*args)))
    scrapeinventory.refresh_now()
    before = len(calls)
    assert before > 0
    scrapeinventory.refresh_now(incremental=True)
    assert len(calls) == before
    old_stat = path.stat(); replacement = path.with_suffix('.tmp'); replacement.write_bytes(path.read_bytes()); os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns)); os.replace(replacement, path)
    scrapeinventory.refresh_now(incremental=True)
    assert len(calls) == before+1, 'Same size and mtime must not hide a different inode'


def test_shared_heavy_budget_is_bounded_and_reentrant():
    from app.services.resourcebudget import slot
    from concurrent.futures import ThreadPoolExecutor
    active = peak = 0
    lock = threading.Lock()
    def work():
        nonlocal active, peak
        with slot(), slot():
            with lock: active += 1; peak = max(peak, active)
            time.sleep(.02)
            with lock: active -= 1
    with ThreadPoolExecutor(max_workers=6) as pool: list(pool.map(lambda _:work(), range(6)))
    assert peak == 2


def test_existing_identical_webp_finishes_interrupted_format_migration(pictures):
    root, _, _ = pictures
    old = root/'ABC-001'/'banner.jpg'; old.parent.mkdir(); old.write_bytes(jpeg())
    encoded, suffix = imagecache.optimized(old.read_bytes())
    target = old.with_suffix(suffix); target.write_bytes(encoded)
    before = cachemaintenance._fingerprint(target)
    data = task('optimize'); cachemaintenance._run(data)
    assert data['changed'] == 1 and not old.exists()
    assert cachemaintenance._fingerprint(target) == before


def test_actor_photo_selection_is_bounded_even_when_first_page_has_invalid_names(db, monkeypatch):
    from app import services
    from app.database.models import Actor
    from sqlalchemy import event
    stamp = datetime(2025,1,1)
    with db.begin() as session:
        session.add_all([Actor(name=str(i),subscribed=True,update_time=stamp) for i in range(250)] +
            [Actor(name=f'Actor {i:03}',subscribed=True,update_time=stamp) for i in range(60)])
    queries=[]
    engine=db.kw['bind']
    def capture(conn,cursor,statement,parameters,context,executemany):
        if 'FROM actor' in statement:queries.append(statement)
    event.listen(engine,'before_cursor_execute',capture)
    selected=[]
    monkeypatch.setattr(services,'fill_lack_actors_by_list',lambda names:(selected.extend(names) or len(names)))
    try:
        assert services.fill_lack_actors(50) == 50
        assert len(queries)==2 and all('LIMIT' in query for query in queries)
        assert all(name.startswith('Actor ') for name in selected)
    finally:
        event.remove(engine,'before_cursor_execute',capture)


def test_portrait_bulk_update_preserves_other_metadata_and_ignores_removed_rows(db):
    from app.services.portrait import _flush
    with db.begin() as session:session.add(Code(code='ABC-001',title='kept'))
    _flush([('ABC-001','right'),('MISSING','left')])
    with db() as session:
        row=session.get(Code,'ABC-001')
        assert row.portrait_side=='right' and row.title=='kept'
