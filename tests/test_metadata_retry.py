"""Automatic metadata backoff survives restart and never blocks manual lookup."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import importlib
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from app import services
from app.database import session as database
from app.database.base import DBBase
from app.database.models import Cache, Code
from app.modules import ladysite
from app.modules.ladysite import base, transport
from app.services import metadataretry as retry


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'retry.db'}", connect_args={"check_same_thread": False})
    DBBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(services, "get_settings", lambda: SimpleNamespace(bypass_url="http://trawl:8191"))
    yield factory
    engine.dispose()


def add(db, *codes):
    with db.begin() as session:
        session.add_all(Code(code=code) for code in codes)


def state(db, code):
    with db() as session:
        return session.scalar(select(Cache).where(Cache.namespace == retry.NAMESPACE, Cache.key == code))


def expire(db, code):
    with db.begin() as session:
        session.execute(update(Cache).where(Cache.namespace == retry.NAMESPACE, Cache.key == code).values(
            create_time=datetime.now() - timedelta(seconds=1)))


@pytest.mark.parametrize("retryable,seconds,reason", [(False, 21600, "empty"), (True, 600, "error")])
def test_empty_or_failed_lookups_are_persisted_and_retry_after_expiry(db, monkeypatch, retryable, seconds, reason):
    add(db, "ABC-101")
    calls = []
    def fetch(code):
        calls.append(code)
        return transport.DetailResult(retryable=retryable)
    monkeypatch.setattr(ladysite, "get_code_detail", fetch)
    assert services.fill_lack_codes_by_list(["ABC-101", "ABC-101"]) == 0
    row = state(db, "ABC-101")
    assert json.loads(row.content)["reason"] == reason
    assert seconds - 5 < (row.create_time - datetime.now()).total_seconds() <= seconds
    # Reloading the admission module has no effect on the durable cooldown.
    importlib.reload(retry)
    assert services.fill_lack_codes() == 0
    assert services.fill_lack_codes_by_list(["ABC-101"]) == 0
    assert calls == ["ABC-101"]
    expire(db, "ABC-101")
    monkeypatch.setattr(ladysite, "get_code_detail", lambda code: {"code": "ALIAS-999", "title": "Found", "banner": "https://source.test/cover.jpg"})
    assert services.fill_lack_codes() == 1
    with db() as session:
        assert session.get(Code, "ABC-101").title == "Found"
        assert session.get(Code, "ALIAS-999") is None
    assert state(db, "ABC-101") is None


def test_cooling_codes_do_not_consume_batch_limit(db, monkeypatch):
    add(db, "ABC-101", "ABC-102", "ABC-103")
    retry.defer("ABC-101", retryable=False)
    retry.defer("ABC-102", retryable=False)
    calls = []
    monkeypatch.setattr(ladysite, "get_code_detail", lambda code: calls.append(code) or {"title": "Found"})
    assert services.fill_lack_codes(limit=1) == 1
    assert calls == ["ABC-103"]


def test_listing_skips_cooling_entries_before_its_fetch_limit(db, monkeypatch):
    add(db, "ABC-101", "ABC-102")
    retry.defer("ABC-101", retryable=False)
    calls = []
    monkeypatch.setattr(ladysite, "get_code_detail", lambda code: calls.append(code) or {"title": "Found"})
    items = services.enrich_codes(["ABC-101", "ABC-102"], fetch_limit=1)
    assert calls == ["ABC-102"]
    assert items[1]["title"] == "Found"


def test_partial_success_keeps_existing_fields_and_cools_missing_artwork(db, monkeypatch):
    with db.begin() as session:
        session.add(Code(code="ABC-101", title="User title", casts="User cast"))
    calls = []
    def fetch(code):
        calls.append(code)
        return {"title": "Source title", "casts": "Other cast", "outline": "New synopsis"}
    monkeypatch.setattr(ladysite, "get_code_detail", fetch)
    assert services.fill_lack_codes_by_list(["ABC-101"]) == 1
    assert services.fill_lack_codes_by_list(["ABC-101"]) == 0
    assert calls == ["ABC-101"]
    with db() as session:
        row = session.get(Code, "ABC-101")
        assert row.title == "User title" and row.casts == "User cast" and row.outline == "New synopsis"
    assert json.loads(state(db, "ABC-101").content)["reason"] == "empty"


def test_concurrent_enrichment_does_not_repeat_inflight_code(db, monkeypatch):
    add(db, "ABC-101")
    entered, release = threading.Event(), threading.Event()
    calls = []
    def fetch(code):
        calls.append(code)
        entered.set()
        assert release.wait(3)
        return {"title": "Found"}
    monkeypatch.setattr(ladysite, "get_code_detail", fetch)
    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(services.fill_lack_codes_by_list, ["ABC-101"])
            assert entered.wait(2)
            second = pool.submit(services.fill_lack_codes_by_list, ["ABC-101"])
            assert second.result(timeout=2) == 0
            release.set()
            assert first.result(timeout=2) == 1
    finally:
        release.set()
    assert calls == ["ABC-101"]


def test_atomic_claim_and_abandoned_lease_recovery(db):
    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(retry.claim, ["ABC-101"] * 4)) == 1
    expire(db, "ABC-101")
    assert retry.claim("ABC-101")


def test_exception_gets_short_backoff_and_does_not_stop_next_code(db, monkeypatch):
    add(db, "ABC-101", "ABC-102")
    def fetch(code):
        if code == "ABC-101":
            raise TimeoutError("solver unavailable")
        return {"title": "Found"}
    monkeypatch.setattr(ladysite, "get_code_detail", fetch)
    assert services.fill_lack_codes() == 1
    assert json.loads(state(db, "ABC-101").content)["reason"] == "error"


def test_manual_search_bypasses_automatic_backoff(db, monkeypatch):
    retry.defer("ABC-101", retryable=False)
    monkeypatch.setattr(ladysite, "_sites_for_code", lambda code: ("source",))
    monkeypatch.setattr(ladysite, "_fetch_detail", lambda site, code: {"title": "Found"})
    assert ladysite.search_code("ABC-101") == [{"title": "Found"}]


@pytest.mark.parametrize("status,retryable", [(200, False), (404, False), (410, False), (401, True), (403, True), (500, True)])
@pytest.mark.parametrize("budgeted", [False, True])
def test_final_http_outcome_distinguishes_absence_from_failure(monkeypatch, status, retryable, budgeted):
    import time
    client = base.SiteClient("https://source.test", interval=0)
    mock = httpx.MockTransport(lambda request: httpx.Response(status, text="no results"))
    original = httpx.AsyncClient if budgeted else httpx.Client
    def factory(**kwargs):
        return original(**kwargs, transport=mock)
    monkeypatch.setattr(httpx, "AsyncClient" if budgeted else "Client", factory)
    monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(proxy="", bypass_url=""))
    monkeypatch.setattr(client, "_throttle", lambda: None)
    monkeypatch.setattr(base, "CONNECT_RETRIES", 0)
    site = SimpleNamespace(client=client, crawler_original=lambda code: client.get("/search", allow_bypass=False) and None)
    monkeypatch.setattr(ladysite, "_get_site", lambda name: site)
    if budgeted:
        with transport.FetchBudget(time.perf_counter() + 2).activate():
            result = ladysite._fetch_detail("source", "ABC-101")
    else:
        result = ladysite._fetch_detail("source", "ABC-101")
    assert not result and result.retryable is retryable


def test_mixed_empty_and_failed_sources_get_short_backoff(monkeypatch):
    monkeypatch.setattr(ladysite, "_sites_for_code", lambda code: ("empty", "failed"))
    monkeypatch.setattr(ladysite, "_fetch_detail", lambda name, code: transport.DetailResult(retryable=name == "failed"))
    assert ladysite.get_code_detail("ABC-101").retryable
    monkeypatch.setattr(ladysite, "_fetch_detail", lambda *a: transport.DetailResult(retryable=False))
    assert not ladysite.get_code_detail("ABC-101").retryable


@pytest.mark.parametrize("status,retryable", [(404, False), (403, True), (500, True)])
def test_bypass_target_status_is_not_lost_before_backoff(monkeypatch, status, retryable):
    client = base.SiteClient("https://source.test", interval=0, bypass_first=True)
    mock = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "status": "ok", "solution": {"status": status, "response": "Error page"},
    }))
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(**kwargs, transport=mock))
    monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(proxy="", bypass_url="http://trawl:8191/v1"))
    site = SimpleNamespace(client=client, crawler_original=lambda code: client.get("/movie") and None)
    monkeypatch.setattr(ladysite, "_get_site", lambda name: site)
    result = ladysite._fetch_detail("source", "ABC-101")
    assert not result and result.retryable is retryable
