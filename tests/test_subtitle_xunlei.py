"""Xunlei lookup, candidate validation, configuration and library writes."""
from contextlib import contextmanager

import httpx
import pytest
from sqlalchemy import select

from app.modules import subtitle
from app.modules.subtitle.xunlei import XunleiSubtitle
from app.modules.ladysite import base, sources
from app.database.models import DataSource, MediaLink
from app.database.session import session_scope
from app.services import subtitle as service
from tests.test_subtitle import clean_links, enabled, library, SRT_ZH, SRT_TW, SRT_JA


def item(name="ABS-001.srt", url="https://files.test/sub.srt", ext="srt"):
    return {"name": name, "url": url, "ext": ext}


@pytest.fixture
def http(monkeypatch):
    original = httpx.Client
    requests = []
    def install(data, files=None, api_code=0):
        files = files or {"/sub.srt": SRT_ZH.encode()}
        def respond(request):
            requests.append(request)
            if request.url.path == "/oracle/subtitle":
                return httpx.Response(200, json={"code": api_code, "data": data})
            value = files.get(request.url.path, b"missing")
            if isinstance(value, int):
                return httpx.Response(value)
            return httpx.Response(200, content=value)
        monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)))
        monkeypatch.setattr(base.SiteClient, "_throttle", lambda self: None)
        return requests
    return install


def test_search_uses_base_code_keeps_local_identity_and_downloads_chinese(http):
    requests = http([item()])
    result = XunleiSubtitle(host="https://api.test").search("ABS-001-UC")
    assert result.code == "ABS-001-UC" and result.site == "xunlei" and result.suffix == ".srt"
    assert result.content == SRT_ZH
    assert requests[0].url.params["name"] == "ABS-001"
    assert "gcid" in requests[0].url.params and "cid" in requests[0].url.params


def test_mismatched_or_unidentified_candidates_are_not_downloaded(http):
    requests = http([item("ABS-0011.srt"), item("unrelated.srt"), item("ABS-002.srt")])
    assert XunleiSubtitle(host="https://api.test").search("ABS-001") is None
    assert len(requests) == 1


def test_srt_is_preferred_to_ass_and_download_does_not_forward_source_cookie(http):
    requests = http([item("ABS-001.ass", "https://files.test/sub.ass", "ass"), item()])
    site = XunleiSubtitle(host="https://api.test")
    site.client.cookie = "private=secret"
    assert site.search("ABS-001").suffix == ".srt"
    assert requests[-1].url.path == "/sub.srt"
    assert "private=secret" in requests[0].headers["cookie"]
    assert "cookie" not in requests[-1].headers


@pytest.mark.parametrize("bad", [SRT_JA.encode(), b"<html>error page</html>" * 20, 404, b"[Script Info]\n" + b"x" * 300])
def test_invalid_download_tries_next_candidate_and_converts_traditional(http, bad):
    http([item(url="https://files.test/bad.srt"), item()], {"/bad.srt": bad, "/sub.srt": SRT_TW.encode()})
    result = XunleiSubtitle(host="https://api.test").search("ABS-001")
    assert result and "我们" in result.content and "我們" not in result.content


def test_oversized_download_is_rejected(http, monkeypatch):
    from app.modules.subtitle import xunlei
    monkeypatch.setattr(xunlei, "MAX_SUBTITLE_BYTES", 250)
    http([item()], {"/sub.srt": SRT_ZH.encode() * 100})
    assert XunleiSubtitle(host="https://api.test").search("ABS-001") is None


def test_api_failure_or_malformed_schema_is_not_a_subtitle(http):
    requests = http([item()], api_code=1)
    assert XunleiSubtitle(host="https://api.test").search("ABS-001") is None
    assert len(requests) == 1


def test_config_registers_as_subtitle_respects_disable_and_survives_reload(http):
    from app.api.endpoints.datasource import list_datasources, update_datasource, SourceRequest
    response = list_datasources(current_user="test")
    row = next(row for row in response["data"]["items"] if row["key"] == "xunlei")
    assert row["kind"] == "subtitle" and not row["in_detail"] and row["enabled"]
    try:
        assert update_datasource("xunlei", SourceRequest(enabled=False), "test")["code"] == 200
        assert XunleiSubtitle().search("ABS-001") is None
        assert subtitle._build("xunlei") is None
        assert not next(row for row in list_datasources("test")["data"]["items"] if row["key"] == "xunlei")["enabled"]
    finally:
        update_datasource("xunlei", SourceRequest(enabled=True), "test")


def test_connection_check_queries_the_api_and_persists_status(http):
    sources.sync_builtin_sources()
    requests = http([])
    result = sources.check_source("xunlei")
    assert result["status"] == "ok"
    assert requests[0].url.path == "/oracle/subtitle"
    with session_scope() as session:
        row = session.scalar(select(DataSource).where(DataSource.key == "xunlei"))
        assert row.status == "ok" and row.checked_time


def test_aggregate_uses_xunlei_and_writes_beside_video(http, library, enabled, monkeypatch):
    requests = http([item()])
    monkeypatch.setattr(subtitle, "SUBTITLE_SITES", ("xunlei",))
    assert service.fetch_for_code("ABS-001") == 1
    saved = library.with_name("ABS-001.zh-CN.srt")
    assert saved.read_text(encoding="utf-8") == SRT_ZH
    with session_scope() as session:
        assert session.get(MediaLink, str(library)).has_subtitle
    # Normal automatic runs preserve an existing subtitle and make no request.
    before = len(requests)
    assert service.fetch_for_code("ABS-001") == 0
    assert len(requests) == before


def test_manual_fetch_uses_new_source_when_automatic_switch_is_off(http, library, monkeypatch):
    from app.core.config import get_settings
    http([item()])
    monkeypatch.setattr(get_settings(), "subtitle_enabled", False)
    monkeypatch.setattr(subtitle, "SUBTITLE_SITES", ("xunlei",))
    assert service.fetch_for_code("ABS-001", manual=True) == 1


def test_empty_xunlei_continues_to_next_source_and_parts_do_not_use_whole_movie(monkeypatch):
    calls = []
    class Source:
        def __init__(self, key): self.key = key
        def search(self, code, **kwargs):
            calls.append(self.key)
            return None
    monkeypatch.setattr(subtitle, "_build", lambda key: Source(key))
    assert subtitle.search("ABS-001") is None
    assert calls.index("subtitlelocal") < calls.index("xunlei") < calls.index("javsub")
    calls.clear()
    assert subtitle.search("ABS-001", part=2) is None
    assert calls == ["subtitlelocal"]


def test_download_uses_the_source_proxy_configuration(http, monkeypatch):
    from app.modules.subtitle import xunlei
    leases = []
    @contextmanager
    def lease(fallback, explicit, key, url):
        leases.append((explicit, key, url))
        yield None
    monkeypatch.setattr(base, "proxy_lease", lease)
    monkeypatch.setattr(xunlei, "proxy_lease", lease)
    http([item()])
    site = XunleiSubtitle(host="https://api.test")
    site.client.proxy = "http://source-proxy.test"
    assert site.search("ABS-001")
    assert len(leases) == 2
    assert all(explicit == "http://source-proxy.test" and key == "xunlei" for explicit, key, _ in leases)
