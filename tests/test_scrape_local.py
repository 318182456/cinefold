"""本地元数据优先、联网补缺与分集登记的回归约束。"""
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import DBBase
from app.database.models import Code
from app.modules.scrape import local, merge
from app.services import scrape
from app.utils.mediafile import parse


@pytest.fixture
def db(monkeypatch):
    import app.database.session as database
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    DBBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    yield factory
    engine.dispose()


def sidecar(tmp_path, *, code="ABS-001", title="本地标题"):
    video = tmp_path / "ABS-001.mp4"
    video.write_bytes(b"video")
    video.with_suffix(".nfo").write_text(
        f'<movie><num>{code}</num><title>{title}</title><originaltitle>Original</originaltitle>'
        '<premiered>2025-01-02</premiered><runtime>120</runtime>'
        '<actor><name>演员甲</name></actor><genre>标签甲</genre><plot>本地简介</plot>'
        '<fanart><thumb>fanart.jpg</thumb></fanart></movie>', encoding="utf-8")
    (tmp_path / "fanart.jpg").write_bytes(b"cover")
    return video


def test_nfo_reads_local_artwork_and_preserves_language(tmp_path):
    video = sidecar(tmp_path)
    from PIL import Image
    image_path = tmp_path / 'fanart.jpg'
    Image.new('RGB', (100, 100)).save(image_path)
    detail = local.read_group("ABS-001", [video])
    assert detail["title"] == "Original"
    assert detail["cn_title"] == "本地标题"
    assert detail["casts"] == "演员甲"
    assert detail["_local_cover"] == str((tmp_path / "fanart.jpg").resolve())
    assert scrape._cover_bytes(detail, "ABS-001") == image_path.read_bytes()


def test_wrong_number_cannot_contaminate_local_metadata(tmp_path):
    video = sidecar(tmp_path, code="ABS-999")
    detail = local.read_group("ABS-001", [video])
    assert "title" not in detail and "casts" not in detail


def test_mixed_directory_does_not_borrow_movie_nfo_or_shared_cover(tmp_path):
    video = tmp_path / "ABS-001.mp4"; video.touch()
    (tmp_path / "ABS-002.mp4").touch()
    (tmp_path / "movie.nfo").write_text('<movie><title>另一部片</title></movie>', encoding="utf-8")
    (tmp_path / "poster.jpg").write_bytes(b"cover")
    assert local.read_group("ABS-001", [video]) == {}


def test_local_cover_cannot_escape_video_directory(tmp_path):
    folder = tmp_path / "videos"; folder.mkdir()
    (tmp_path / "outside.jpg").write_bytes(b"outside")
    nfo = folder / "ABS-001.nfo"
    nfo.write_text('<movie><title>标题</title><thumb>../outside.jpg</thumb></movie>', encoding="utf-8")
    assert "_local_cover" not in local.read(nfo, "ABS-001")


def test_json_aliases_and_seconds_duration(tmp_path):
    path = tmp_path / "ABS-001.json"
    path.write_text('{"number":"ABS-001","title":"标题","actors":[{"name":"演员甲"}],"genres":["标签甲","标签甲"],"runtime":120}', encoding="utf-8")
    result = local.read(path, "ABS-001")
    assert result["casts"] == "演员甲" and result["genres"] == "标签甲"
    path = path.with_suffix('.nfo')
    path.write_text('<movie><title>T</title><durationinseconds>600</durationinseconds></movie>', encoding='utf-8')
    assert local.read(path, "ABS-001")["duration"] == "10"


def test_json_preserves_explicit_translation_and_merges_tags(tmp_path):
    video = tmp_path / 'ABS-001.mp4'; video.touch()
    video.with_suffix('.json').write_text('{"title":"Original","cn_title":"人工译名","genres":["A"],"tags":["A","B"]}', encoding='utf-8')
    (tmp_path / 'poster.png').write_bytes(b'png')
    result = local.read_group('ABS-001', [video])
    assert result['cn_title'] == '人工译名' and result['genres'] == 'A,B'
    assert result['_local_cover'].endswith('poster.png')


def test_malformed_and_entity_nfo_are_ignored(tmp_path):
    path = tmp_path / "ABS-001.nfo"
    path.write_text('<movie><title>broken', encoding="utf-8")
    assert local.read(path, "ABS-001") == {}
    path.write_text('<!DOCTYPE movie [<!ENTITY x "text">]><movie><title>&x;</title></movie>', encoding="utf-8")
    assert local.read(path, "ABS-001") == {}


def test_sidecar_with_space_before_suffix_and_xml_encoding(tmp_path):
    video = tmp_path / 'ABS-001.mp4'; video.touch()
    (tmp_path / 'ABS-001 .nfo').write_bytes('<?xml version="1.0" encoding="gbk"?><movie><title>本地标题</title></movie>'.encode('gbk'))
    assert local.read_group('ABS-001', [video])['title'] == '本地标题'


def test_local_cover_preview_requires_login_and_matches_output(db, tmp_path):
    from io import BytesIO
    from fastapi.testclient import TestClient
    from PIL import Image
    from app.api import create_app
    from app.api.endpoints import get_current_user
    from app.modules.scrape.images import crop_poster
    folder = tmp_path / 'movies'; folder.mkdir()
    video = sidecar(folder)
    output = BytesIO(); Image.new('RGB', (80, 40), 'red').save(output, format='JPEG')
    content = output.getvalue(); (folder / 'fanart.jpg').write_bytes(content)
    app = create_app()
    client = TestClient(app)
    params = {'path': str(video), 'code': 'ABS-001', 'poster': True}
    assert client.get('/api/v1/scrape/local-cover', params=params).status_code == 401
    app.dependency_overrides[get_current_user] = lambda: 'test'
    response = client.get('/api/v1/scrape/local-cover', params=params)
    assert response.status_code == 200 and response.content == crop_poster(content)


def test_complete_local_metadata_skips_network_and_preserves_existing(db, tmp_path, monkeypatch):
    video = sidecar(tmp_path)
    with db.begin() as session:
        session.add(Code(code="ABS-001", cn_title="人工标题", status=2))
    def unexpected(*args, **kwargs):
        raise AssertionError("完整本地元数据不应联网")
    monkeypatch.setattr(merge, "fetch_merged", unexpected)
    detail = scrape.load_group_meta("ABS-001", [parse(video)], fetch=True)
    assert detail["cn_title"] == "人工标题" and detail["casts"] == "演员甲"
    with db() as session:
        assert session.get(Code, "ABS-001").status == 2


def test_existing_title_does_not_stop_missing_field_enrichment(db, monkeypatch):
    with db.begin() as session:
        session.add(Code(code="ABS-001", title="已有标题", status=1))
    monkeypatch.setattr(merge, "fetch_merged", lambda code: {"code": code, "title": "网站新标题", "casts": "演员甲", "status": 4})
    monkeypatch.setattr(scrape, "_translate_quietly", lambda code: None)
    detail = scrape._load_meta("ABS-001")
    assert detail["title"] == "已有标题" and detail["casts"] == "演员甲"
    with db() as session:
        assert session.get(Code, "ABS-001").status == 1


def test_network_disabled_still_reads_local_nfo(db, tmp_path, monkeypatch):
    video = sidecar(tmp_path)
    monkeypatch.setattr(merge, "fetch_merged", lambda *a: pytest.fail("不应联网"))
    assert scrape.load_group_meta("ABS-001", [parse(video)], fetch=False)["outline"] == "本地简介"


def test_remote_wrong_number_and_error_title_are_rejected():
    result = merge.merge_details([
        merge.SourceResult("wrong", {"code": "ABS-999", "title": "错误影片"}),
        merge.SourceResult("blocked", {"title": "Just a moment...", "status": 4}),
        merge.SourceResult("ok", {"title": "正确标题", "casts": "演员甲"}),
    ], "ABS-001")
    assert result == {"title": "正确标题", "casts": "演员甲"}


def test_preview_uses_local_metadata_and_includes_artwork(db, tmp_path, monkeypatch):
    from app.api.endpoints import scrape as api
    from app.core.config import Settings
    folder = tmp_path / 'movies'; folder.mkdir()
    video = sidecar(folder)
    settings = Settings(medialink_scrape_dir=str(tmp_path / "output"))
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(scrape, "get_settings", lambda: settings)
    result = api._preview_sync(api.ScrapeRequest(path=str(video), fetch_meta=False))
    item = result["data"]["items"][0]
    assert "本地标题" in item["nfo"]
    assert item["images"]["poster"].startswith('/api/v1/scrape/local-cover?')
    assert any(x['kind'] == 'poster' for x in item['outputs'])
    assert not (tmp_path / 'output').exists()


def test_skipped_part_does_not_shift_source_registration(tmp_path, monkeypatch, db):
    from app.core.config import Settings
    first = tmp_path / "ABS-001-CD1.mp4"; first.touch()
    second = tmp_path / "ABS-001-CD2.mp4"; second.touch()
    monkeypatch.setattr(scrape, "get_settings", lambda: Settings(medialink_scrape_dir=str(tmp_path / "output")))
    monkeypatch.setattr(scrape, "load_group_meta", lambda *a, **k: {"title": "T"})
    monkeypatch.setattr(scrape, "_cover_bytes", lambda *a: b"")
    monkeypatch.setattr(scrape, "_make_link", lambda source, target: (source == second, "冲突"))
    monkeypatch.setattr(scrape.scrape_images, "write_images", lambda *a, **k: {})
    monkeypatch.setattr(scrape.nfo, "write", lambda *a, **k: True)
    recorded = []
    monkeypatch.setattr(scrape, "_register", lambda code, items, result: recorded.extend(i.path for i in items))
    result = scrape.scrape_group("ABS-001", [parse(first), parse(second)])
    assert len(result.links) == 1 and recorded == [second]


def test_single_file_preview_honors_explicit_number_and_keeps_subtitle_flag(db, tmp_path, monkeypatch):
    from app.api.endpoints import scrape as api
    from app.core.config import Settings
    settings = Settings(medialink_scrape_dir=str(tmp_path / 'library'), scrape_still_limit=0)
    monkeypatch.setattr(api, 'get_settings', lambda: settings)
    monkeypatch.setattr(scrape, 'get_settings', lambda: settings)
    folder = tmp_path / 'movies'; folder.mkdir()
    video = folder / 'MXGS-783-C.mp4'; video.write_bytes(b'video')
    with db() as session:
        session.add(Code(code='MXGS-783', title='基础标题', casts='演员甲'))
        session.commit()
    result = api._preview_sync(api.ScrapeRequest(path=str(video), code='MXGS-783', fetch_meta=False))
    item = result['data']['items'][0]
    assert item['code'] == 'MXGS-783' and item['subbed']
    assert item['has_meta'] and '基础标题' in item['nfo']
    assert '<num>MXGS-783</num>' in item['nfo']


def test_subtitle_version_preview_inherits_base_title_without_changing_output_number(db, tmp_path, monkeypatch):
    from app.api.endpoints import scrape as api
    from app.core.config import Settings
    settings = Settings(medialink_scrape_dir=str(tmp_path / 'library'), scrape_still_limit=0)
    monkeypatch.setattr(api, 'get_settings', lambda: settings)
    monkeypatch.setattr(scrape, 'get_settings', lambda: settings)
    folder = tmp_path / 'movies'; folder.mkdir()
    video = folder / 'MXGS-783-C.mp4'; video.write_bytes(b'video')
    with db() as session:
        session.add(Code(code='MXGS-783', title='基础标题', casts='演员甲', banner='https://cover.test/image.jpg'))
        session.commit()
    result = api._preview_sync(api.ScrapeRequest(path=str(video), fetch_meta=False))
    item = result['data']['items'][0]
    assert item['code'] == 'MXGS-783-C' and item['has_meta']
    assert '<num>MXGS-783-C</num>' in item['nfo'] and '基础标题' in item['nfo']
    assert item['images']['poster']


def test_directory_preview_manual_number_does_not_replace_recognized_movies(db, tmp_path, monkeypatch):
    from app.api.endpoints import scrape as api
    from app.core.config import Settings
    settings = Settings(medialink_scrape_dir=str(tmp_path / 'library'))
    monkeypatch.setattr(api, 'get_settings', lambda: settings)
    monkeypatch.setattr(scrape, 'get_settings', lambda: settings)
    (tmp_path / 'MXGS-783-C.mp4').write_bytes(b'video')
    (tmp_path / 'unknown.mp4').write_bytes(b'video')
    result = api._preview_sync(api.ScrapeRequest(path=str(tmp_path), code='SONE-725', fetch_meta=False))
    assert {item['code'] for item in result['data']['items']} == {'MXGS-783-C', 'SONE-725'}
