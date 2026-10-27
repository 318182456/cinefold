"""Strict full decoding, bounded fingerprint caches and restart-safe DB reuse."""
from contextlib import contextmanager
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database.models import Cache
from app.modules.scrape import images
from app.services import artifactvalidation as validation


def picture():
    stream = BytesIO()
    with Image.new('RGB', (300, 200), 'blue') as image:
        image.save(stream, 'JPEG')
    return stream.getvalue()


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'validation.db'))
    Cache.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    @contextmanager
    def scope():
        with factory() as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise
    monkeypatch.setattr(validation, 'session_scope', scope)
    monkeypatch.setattr(validation, '_db_retry_at', 0)
    validation._validated.cache_clear()
    images._valid_file.cache_clear()
    yield scope
    validation._validated.cache_clear()
    images._valid_file.cache_clear()
    engine.dispose()


def test_validity_does_not_reencode_and_unchanged_file_is_not_read_twice(tmp_path, monkeypatch):
    path = tmp_path / 'poster.jpg'
    path.write_bytes(picture())
    calls = []
    original = Image.open
    monkeypatch.setattr(images, 'jpeg_bytes', lambda *a: pytest.fail('validation must not reencode artwork'))
    monkeypatch.setattr(Image, 'open', lambda *a, **kw: calls.append(1) or original(*a, **kw))
    assert images.valid_file(path) and images.valid_file(path)
    assert calls == [1]
    path.write_bytes(b'corrupt')
    assert not images.valid_file(path)
    path.unlink()
    assert not images.valid_file(path)
    path.write_bytes(picture())
    assert images.valid_file(path)
    assert len(calls) == 3


def test_restart_reuses_persisted_fingerprint_without_opening_image(db, tmp_path, monkeypatch):
    path = tmp_path / 'poster.jpg'
    path.write_bytes(picture())
    assert validation.valid_image(path)
    validation._validated.cache_clear()
    monkeypatch.setattr(Image, 'open', lambda *a, **kw: pytest.fail('unchanged persisted image must not be reread'))
    assert validation.valid_image(path)
    with db() as session:
        assert len(session.scalars(select(Cache)).all()) == 1


def test_replaced_same_size_and_mtime_file_is_revalidated(db, tmp_path):
    import os
    path = tmp_path / 'poster.jpg'
    data = picture()
    path.write_bytes(data)
    before = path.stat()
    assert validation.valid_image(path)
    replacement = tmp_path / 'replacement.jpg'
    replacement.write_bytes(b'x' * len(data))
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(path)
    assert not validation.valid_image(path)
    assert images._valid_file.cache_info().maxsize == 8192
    assert validation._validated.cache_info().maxsize == 8192


@pytest.mark.parametrize('data', [b'not an image', picture()[:150], picture()[:-200]])
def test_corrupt_and_truncated_images_remain_invalid(db, tmp_path, data):
    path = tmp_path / 'poster.jpg'
    path.write_bytes(data)
    assert not images.valid_file(path)
    assert not validation.valid_image(path)


def test_database_outage_falls_back_and_cools_down(db, tmp_path, monkeypatch):
    calls = []
    @contextmanager
    def unavailable():
        calls.append(1)
        raise RuntimeError('database unavailable')
        yield
    monkeypatch.setattr(validation, 'session_scope', unavailable)
    first, second = tmp_path / 'one.jpg', tmp_path / 'two.jpg'
    first.write_bytes(picture())
    second.write_bytes(picture())
    assert validation.valid_image(first)
    assert validation.valid_image(second)
    assert calls == [1]


def test_transient_decode_failure_is_not_cached_as_permanently_invalid(db, tmp_path, monkeypatch):
    path = tmp_path / 'poster.jpg'
    path.write_bytes(picture())
    original = images._decode_file
    calls = []
    def decode(*args):
        calls.append(1)
        return False if len(calls) == 1 else original(*args)
    monkeypatch.setattr(images, '_decode_file', decode)
    assert not validation.valid_image(path)
    assert validation.valid_image(path)
    assert len(calls) == 2
