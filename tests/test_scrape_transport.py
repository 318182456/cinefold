"""Exercise real async requests, cancellation and shared scrape deadlines."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from app.modules.ladysite import base, transport
from app.modules.ladysite.transport import FetchBudget


@pytest.fixture
def mock_http(monkeypatch):
    original = httpx.AsyncClient
    options = []
    clients = []

    def install(handler):
        def factory(**kwargs):
            options.append(kwargs.copy())
            kwargs["transport"] = httpx.MockTransport(handler)
            client = original(**kwargs)
            clients.append(client)
            return client
        monkeypatch.setattr(transport.httpx, "AsyncClient", factory)
        return options
    monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(proxy="", bypass_url=""))
    base.SiteClient.reset_throttle()
    base._BYPASS_KIND.clear()
    install.clients = clients
    return install


def call(client, budget):
    with budget.activate():
        return client.get("/movie", timeout=10)


def test_cancel_interrupts_inflight_io_and_closes_client(mock_http):
    started, closed = threading.Event(), threading.Event()

    async def handler(request):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            closed.set()

    mock_http(handler)
    budget = FetchBudget(time.perf_counter() + 10)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(call, base.SiteClient("https://source.test"), budget)
        assert started.wait(2)
        start = time.perf_counter()
        budget.cancel()
        assert future.result(timeout=1) == ""
        assert closed.wait(1)
        for _ in range(100):
            if all(client.is_closed for client in mock_http.clients):
                break
            time.sleep(0.005)
        assert mock_http.clients and all(client.is_closed for client in mock_http.clients)
        assert time.perf_counter() - start < 1


def test_deadline_stops_blocked_async_request(mock_http):
    closed = threading.Event()

    async def handler(request):
        try:
            await asyncio.sleep(60)
        finally:
            closed.set()

    mock_http(handler)
    start = time.perf_counter()
    assert call(base.SiteClient("https://source.test"), FetchBudget(start + 0.15)) == ""
    assert time.perf_counter() - start < 1
    assert closed.wait(1)


@pytest.mark.parametrize("lock_kind", ["throttle", "bypass"])
def test_cancel_interrupts_shared_lock_queue(mock_http, monkeypatch, lock_kind):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, text="unexpected")

    mock_http(handler)
    client = base.SiteClient("https://source.test", bypass_first=lock_kind == "bypass")
    if lock_kind == "bypass":
        monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(proxy="", bypass_url="http://solver/v1"))
        lock = base._BYPASS_LOCK
    else:
        lock = client._state()[0]
    budget = FetchBudget(time.perf_counter() + 10)
    lock.acquire()
    try:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(call, client, budget)
            time.sleep(0.08)
            budget.cancel()
            assert future.result(timeout=1) == ""
    finally:
        lock.release()
    assert requests == []


def test_throttle_sleep_obeys_total_deadline(mock_http):
    async def unexpected(request):
        pytest.fail("network must not start after waiting past deadline")

    mock_http(unexpected)
    client = base.SiteClient("https://source.test", interval=60)
    client._state()[1] = time.time()
    start = time.perf_counter()
    assert call(client, FetchBudget(start + 0.1)) == ""
    assert time.perf_counter() - start < 1


def test_bypass_probe_and_request_share_budget(mock_http, monkeypatch):
    bodies = []
    cancelled = threading.Event()

    async def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if not body["url"]:
            await asyncio.sleep(0.06)
            return httpx.Response(400)
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    mock_http(handler)
    monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(proxy="", bypass_url="http://solver"))
    client = base.SiteClient("https://source.test", bypass_first=True)
    start = time.perf_counter()
    assert call(client, FetchBudget(start + 0.2)) == ""
    assert cancelled.wait(1)
    assert len(bodies) == 2
    assert 0 < bodies[1]["maxTimeout"] < 180
    assert time.perf_counter() - start < 1


def test_transient_status_retries_once_preserving_headers_and_final_url(mock_http, monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, text="detail")

    options = mock_http(handler)
    monkeypatch.setattr(base.random, "uniform", lambda *_: 0)
    client = base.SiteClient("https://source.test", cookie="age=1", interval=0)
    assert call(client, FetchBudget(time.perf_counter() + 3)) == "detail"
    assert len(requests) == 2
    assert requests[1].headers["cookie"] == "age=1"
    assert requests[1].headers["referer"] == "https://source.test/"
    assert client.last_url == "https://source.test/movie"
    assert all(option["trust_env"] is False for option in options)


@pytest.mark.parametrize("status,retry_after", [(404, "0"), (429, "10000")])
def test_no_retry_on_missing_page_or_insufficient_budget(mock_http, status, retry_after):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Retry-After": retry_after})

    mock_http(handler)
    assert call(base.SiteClient("https://source.test"), FetchBudget(time.perf_counter() + 0.5)) == ""
    assert len(requests) == 1


def test_separate_sync_workers_share_one_async_loop(mock_http):
    loops = set()
    entered = 0

    async def handler(request):
        nonlocal entered
        loops.add(id(asyncio.get_running_loop()))
        entered += 1
        while entered < 3:
            await asyncio.sleep(0.005)
        return httpx.Response(200, text="ok")

    mock_http(handler)
    budget = FetchBudget(time.perf_counter() + 3)
    with ThreadPoolExecutor(3) as pool:
        futures = [pool.submit(call, base.SiteClient(f"https://source{i}.test"), budget) for i in range(3)]
        assert [future.result(timeout=3) for future in futures] == ["ok"] * 3
    assert len(loops) == 1
    assert transport.current_budget() is None


def test_direct_timeout_does_not_start_expensive_solver(mock_http, monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("blocked direct route", request=request)

    mock_http(handler)
    monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(proxy="", bypass_url="http://solver/v1"))
    client = base.SiteClient("https://source.test", interval=0)
    assert call(client, FetchBudget(time.perf_counter() + 0.2)) == ""
    assert len(requests) == 1
    assert requests[0].url.host == "source.test"


def test_bypass_preserves_final_url_cookie_and_selected_proxy(mock_http, monkeypatch):
    bodies = []

    async def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "solution": {"response": "detail", "url": "https://source.test/detail"}})

    options = mock_http(handler)
    monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(proxy="http://global:8888", bypass_url="http://solver/v1"))
    client = base.SiteClient("https://source.test", cookie="age=1", bypass_first=True)
    client.proxy = "http://user:pass@proxy:9999"
    assert call(client, FetchBudget(time.perf_counter() + 2)) == "detail"
    assert client.last_url == "https://source.test/detail"
    assert bodies[0]["cookies"] == [{"name": "age", "value": "1"}]
    assert bodies[0]["proxy"] == {"url": "http://proxy:9999", "username": "user", "password": "pass"}
    assert all("proxy" not in option for option in options)
