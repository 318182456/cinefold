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


def test_bypass_preserves_final_url_cookie_without_sending_proxy(mock_http, monkeypatch):
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
    assert "proxy" not in bodies[0]
    assert all("proxy" not in option for option in options)


@pytest.mark.parametrize("budgeted", [False, True])
@pytest.mark.parametrize("tls_reason", ["SSLV3_ALERT_HANDSHAKE_FAILURE", "TLSV1_UNRECOGNIZED_NAME", "UNEXPECTED_EOF_WHILE_READING"])
@pytest.mark.parametrize("mode", ["bypass", "disabled", "unconfigured"])
def test_tls_retry_falls_back_to_solver_only_when_allowed(monkeypatch, budgeted, tls_reason, mode):
    import ssl
    from contextlib import nullcontext

    requests, bodies = [], []
    original_client = httpx.Client

    def respond(request):
        requests.append(request)
        if request.url.host == "source.test":
            tls = ssl.SSLError(f"[SSL: {tls_reason}] test failure")
            raise httpx.ConnectError("wrapped transport failure", request=request) from tls
        assert request.url.host == "solver"
        body = json.loads(request.content)
        bodies.append(body)
        assert "proxy" not in body
        assert body["cookies"] == [{"name": "age", "value": "1"}]
        assert body["url"] == "https://source.test/api?lng=zh-TW"
        return httpx.Response(200, json={"status": "ok", "solution": {"response": "recovered", "url": "https://source.test/detail"}})

    def factory(**kwargs):
        return original_client(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(base.httpx, "Client", factory)
    monkeypatch.setattr(base, "http_client", factory)
    monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(proxy="http://global:8888", bypass_url="" if mode == "unconfigured" else "http://solver/v1"))
    client = base.SiteClient("https://source.test", cookie="age=1")
    client.proxy = "http://source-proxy:8888"
    monkeypatch.setattr(client, "_throttle", lambda: None)
    budget = FetchBudget(time.perf_counter() + 3)
    monkeypatch.setattr(budget, "wait", lambda seconds: budget.check())
    with budget.activate() if budgeted else nullcontext():
        result = client.get("/api", params={"lng": "zh-TW"}, allow_bypass=mode != "disabled")
    assert sum(request.url.host == "source.test" for request in requests) == 2
    if mode == "bypass":
        assert result == "recovered"
        assert len(bodies) == 1
        assert client.last_url == "https://source.test/detail"
    else:
        assert result == ""
        assert bodies == []


def test_source_connection_check_uses_solver_after_tls_failure(monkeypatch):
    import ssl
    from app.database import models  # noqa: F401
    from app.database.base import DBBase
    from app.database.session import engine
    from app.modules.ladysite import sources

    DBBase.metadata.create_all(engine)
    original_client = httpx.Client
    bodies = []

    def respond(request):
        if request.url.host == "source.test":
            raise httpx.ConnectError("[SSL: TLSV1_UNRECOGNIZED_NAME] test failure", request=request) from ssl.SSLError("test")
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "solution": {"response": "detail"}})

    monkeypatch.setattr(base.httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(respond)))
    from app.core import config
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(proxy="", bypass_url="http://solver/v1"))
    monkeypatch.setattr(base, "get_settings", config.get_settings)
    monkeypatch.setattr(sources, "get_source", lambda key: {"host": "https://source.test", "proxy": "http://source-proxy:8888", "enabled": False})
    result = sources.check_source("tls-test")
    assert result["status"] == "ok"
    assert len(bodies) == 1
    assert "proxy" not in bodies[0]


@pytest.mark.parametrize("budgeted", [False, True])
@pytest.mark.parametrize("recover", [True, False])
@pytest.mark.parametrize("allow_bypass", [False, True])
def test_misdirected_request_reconnects_before_solver(monkeypatch, budgeted, recover, allow_bypass):
    from contextlib import nullcontext
    requests = []
    original_client = httpx.Client

    def respond(request):
        requests.append(request)
        if len(requests) == 1 or not recover and request.url.host == "source.test":
            return httpx.Response(421)
        if request.url.host == "solver":
            return httpx.Response(200, json={"status": "ok", "solution": {"response": "solver detail"}})
        return httpx.Response(200, text="direct detail")

    factory = lambda **kw: original_client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(base.httpx, "Client", factory)
    monkeypatch.setattr(base, "http_client", factory)
    monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(proxy="", bypass_url="http://solver/v1"))
    client = base.SiteClient("https://source.test")
    monkeypatch.setattr(client, "_throttle", lambda: None)
    budget = FetchBudget(time.perf_counter() + 3)
    monkeypatch.setattr(budget, "wait", lambda seconds: budget.check())
    with budget.activate() if budgeted else nullcontext():
        result = client.get("/movie", allow_bypass=allow_bypass)
    assert [r.url.host for r in requests[:2]] == ["source.test", "source.test"]
    if recover:
        assert result == "direct detail" and len(requests) == 2
    elif allow_bypass:
        assert result == "solver detail" and len(requests) == 3
    else:
        assert result == "" and len(requests) == 2


@pytest.mark.parametrize("payload,expected", [
    ({"status": "ok", "solution": {"status": 404, "response": "<h1>Not found</h1>"}}, ""),
    ({"status": "error", "message": "Scrape deadline exceeded", "solution": {"response": "partial content"}}, ""),
    ({"status": "ok", "solution": {"status": 200, "response": '<html><body><pre>{"data":{"barcode":"SSIS-001"}}</pre></body></html>'}}, '{"data": {"barcode": "SSIS-001"}}'),
])
def test_solver_response_status_and_json_wrapping(monkeypatch, payload, expected):
    original_client = httpx.Client
    factory = lambda **kw: original_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    monkeypatch.setattr(base, "http_client", factory)
    monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(bypass_url="http://solver/v1"))
    assert base.fetch_via_bypass("https://source.test/api") == expected


def test_solver_http_failure_log_contains_target_and_reason(monkeypatch):
    original_client = httpx.Client
    factory = lambda **kw: original_client(transport=httpx.MockTransport(lambda request: httpx.Response(500, json={"status": "error", "message": "Scrape deadline exceeded"})))
    monkeypatch.setattr(base, "http_client", factory)
    monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(bypass_url="http://solver/v1"))
    messages = []
    monkeypatch.setattr(base.logger, "warning", messages.append)
    assert base.fetch_via_bypass("https://source.test/works/date") == ""
    assert "https://source.test/works/date" in messages[0]
    assert "Scrape deadline exceeded" in messages[0]


def test_unbudgeted_solver_queue_has_deadline(monkeypatch):
    monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(bypass_url="http://queue-test/v1"))
    monkeypatch.setattr(base, "BYPASS_QUEUE_TIMEOUT", 0.05)
    original_client = httpx.Client
    monkeypatch.setattr(base, "http_client", lambda **kw: original_client(transport=httpx.MockTransport(
        lambda request: pytest.fail("queue timeout must not send HTTP"))))
    started = time.perf_counter()
    base._BYPASS_LOCK.acquire()
    try:
        assert base.fetch_via_bypass("https://source.test/movie") == ""
    finally:
        base._BYPASS_LOCK.release()
    assert time.perf_counter() - started < 0.5


def test_solver_disconnect_pauses_then_recovers(monkeypatch):
    monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(bypass_url="http://recovery-test/v1"))
    monkeypatch.setattr(base, "_BYPASS_RECOVERY", {})
    original_client = httpx.Client
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response")
        return httpx.Response(200, json={"status": "ok", "solution": {"response": "recovered"}})

    monkeypatch.setattr(base, "http_client", lambda **kw: original_client(transport=httpx.MockTransport(respond)))
    assert base.fetch_via_bypass("https://source.test/movie") == ""
    assert base.fetch_via_bypass("https://other.test/movie") == ""
    assert len(calls) == 1
    base._BYPASS_RECOVERY["http://recovery-test/v1"] = 0
    assert base.fetch_via_bypass("https://source.test/movie") == "recovered"
    assert len(calls) == 2


@pytest.mark.parametrize("sites", [("airav",), ("airav", "javdb")])
def test_legacy_detail_deadline_cancels_io_and_queue(mock_http, monkeypatch, sites):
    from app.modules import ladysite
    cancelled = threading.Event()

    async def handler(request):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    mock_http(handler)
    monkeypatch.setattr(ladysite, "_sites_for_code", lambda code: sites)
    monkeypatch.setattr(ladysite, "DETAIL_TIMEOUT_SECONDS", 0.15)

    def fetch(site, code):
        return {"title": "unexpected"} if base.SiteClient("https://source.test").get("/movie") else {}

    monkeypatch.setattr(ladysite, "_fetch_detail", fetch)
    started = time.perf_counter()
    assert ladysite.get_code_detail("WAAA-687") == {}
    assert time.perf_counter() - started < 1
    assert cancelled.wait(1)


def test_legacy_detail_winner_cancels_losing_request(mock_http, monkeypatch):
    from app.modules import ladysite
    active, cancelled = threading.Event(), threading.Event()

    async def handler(request):
        active.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    mock_http(handler)
    monkeypatch.setattr(ladysite, "_sites_for_code", lambda code: ("slow", "fast"))

    def fetch(site, code):
        if site == "slow":
            base.SiteClient("https://source.test").get("/movie")
            return {}
        assert active.wait(1)
        return {"code": code, "title": "winner"}

    monkeypatch.setattr(ladysite, "_fetch_detail", fetch)
    assert ladysite.get_code_detail("WAAA-687")["title"] == "winner"
    assert cancelled.wait(1)
