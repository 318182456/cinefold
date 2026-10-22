"""Real preflight I/O (mocked targets), admission, cache and cancellation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from urllib.parse import urlparse

import httpx
import pytest

from app.services.mihomo import Mihomo, FIRST_PORT
from app.services import mihomo, mihomoprobe
from app.modules.ladysite.transport import FetchBudget, FetchCancelled


@pytest.fixture
def manager(tmp_path, monkeypatch):
    value = Mihomo(tmp_path)
    value.nodes = [{"name": f"Node {index}", "type": "http", "server": "127.0.0.1", "port": 8000 + index} for index in range(3)]
    value.config.update(enabled=True, automatic=True, selected="Node 0")
    monkeypatch.setattr(mihomo, "_instance", value)
    yield value
    value.shutdown()


@pytest.fixture
def network(monkeypatch):
    original = httpx.AsyncClient
    requests, clients = [], []
    def install(handler):
        def factory(**options):
            proxy = options.pop("proxy")
            index = urlparse(proxy).port - FIRST_PORT
            async def respond(request):
                requests.append((index, request.method, str(request.url), proxy))
                return await handler(index, request)
            client = original(**options, transport=httpx.MockTransport(respond))
            clients.append(client)
            return client
        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return requests, clients
    return install


def test_failed_first_node_is_never_assigned_and_success_is_cached(manager, network):
    async def target(index, request):
        if index == 0:
            raise httpx.ConnectError("broken exit")
        await asyncio.sleep(0.02 if index == 1 else 0.1)
        return httpx.Response(200)
    requests, clients = network(target)
    url = "https://source.example/sensitive-path?token=hidden"
    with manager.lease(url=url, remote=True) as proxy:
        assert urlparse(proxy).port == FIRST_PORT + 1
        assert urlparse(proxy).hostname == "cinefold"
    before = len(requests)
    with manager.lease(url=url) as proxy:
        assert urlparse(proxy).port == FIRST_PORT + 1
    assert len(requests) == before
    assert all(method == "HEAD" and target == "https://source.example/" and urlparse(proxy).hostname == "127.0.0.1" for _, method, target, proxy in requests)
    assert all(client.is_closed for client in clients)
    assert not manager.allocated


def test_parallel_probes_pick_the_first_healthy_exit_instead_of_list_order(manager, network):
    async def target(index, request):
        await asyncio.sleep([0.2, 0.1, 0.01][index])
        return httpx.Response(204)
    network(target)
    with manager.lease(url="https://source.example/") as proxy:
        assert urlparse(proxy).port == FIRST_PORT + 2


@pytest.mark.parametrize("status,reachable", [(200, True), (302, True), (401, True), (403, True), (404, True), (405, True), (407, False), (429, False), (502, False)])
def test_target_http_status_is_distinct_from_connection_failure(manager, network, status, reachable):
    manager.config["automatic"] = False
    async def target(index, request): return httpx.Response(status)
    network(target)
    if reachable:
        with manager.lease(url="https://source.example/") as proxy:
            assert proxy
    else:
        with pytest.raises(RuntimeError, match="预检失败"):
            with manager.lease(url="https://source.example/"):
                pytest.fail("failed exit must never be assigned")
    assert not manager.allocated


def test_results_are_per_target_and_expiry_rechecks_the_node(manager, network):
    manager.config["automatic"] = False
    async def target(index, request): return httpx.Response(200)
    requests, _ = network(target)
    with manager.lease(url="https://one.example/"): pass
    with manager.lease(url="https://two.example/"): pass
    assert len(requests) == 2
    # Avoid the independent two-start burst limit while testing cache expiry.
    manager.recent_requests.clear()
    manager.probes = {key: (value[0], value[1] - 121, *value[2:]) for key, value in manager.probes.items()}
    with manager.lease(url="https://one.example/"): pass
    assert len(requests) == 3


def test_all_failed_exits_fail_before_any_business_request(manager, network):
    async def target(index, request): raise httpx.ConnectError("offline")
    requests, _ = network(target)
    with pytest.raises(RuntimeError, match="近期均失败"):
        with manager.lease(url="https://source.example/"):
            pytest.fail("unreachable pool cannot be used")
    assert len(requests) == 3 and not manager.allocated


def test_cancellation_stops_checks_closes_clients_and_releases_nodes(manager, network):
    entered, closed = threading.Event(), threading.Event()
    async def target(index, request):
        entered.set()
        try:
            await asyncio.sleep(60)
        finally:
            closed.set()
    _, clients = network(target)
    budget = FetchBudget(time.perf_counter() + 10)
    def run():
        with budget.activate(), manager.lease(url="https://source.example/"):
            pytest.fail("cancelled probe cannot assign a node")
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run)
        assert entered.wait(2)
        budget.cancel()
        with pytest.raises(FetchCancelled): future.result(timeout=1)
    assert closed.wait(1)
    assert not manager.allocated and not manager.health
    deadline = time.monotonic() + 1
    while not all(client.is_closed for client in clients) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert all(client.is_closed for client in clients)


def test_candidate_groups_still_limit_preflight(manager, network):
    manager.config.update(groups={"Only third": ["Node 2"]}, source_groups={"source": "Only third"})
    async def target(index, request): return httpx.Response(200)
    requests, _ = network(target)
    with manager.lease(source_key="source", url="https://source.example/") as proxy:
        assert urlparse(proxy).port == FIRST_PORT + 2
    assert [index for index, *_ in requests] == [2]


def test_real_request_failure_invalidates_successful_preflight(manager, network):
    manager.config["automatic"] = False
    async def target(index, request): return httpx.Response(200)
    requests, _ = network(target)
    url = "https://source.example/"
    with manager.lease(url=url) as proxy: pass
    assert not manager._preflight_needed(proxy, url)
    manager.note_result(url, proxy, False)
    assert manager._preflight_needed(proxy, url)


def test_status_exposes_connection_result_without_proxy_credentials(manager, network):
    import json
    manager.config["automatic"] = False
    async def target(index, request): return httpx.Response(403)
    network(target)
    with manager.lease(url="https://source.example/private?token=secret"): pass
    status = manager.status()
    assert status["preflight"][0]["host"] == "source.example"
    assert status["preflight"][0]["reachable"] and status["preflight"][0]["status_code"] == 403
    assert manager.credentials["password"] not in json.dumps(status)
    assert "/private" not in json.dumps(status)


def test_preflight_timeout_does_not_dispatch_or_leave_reserved_nodes(manager, network, monkeypatch):
    monkeypatch.setattr(mihomoprobe, "TIMEOUT", 0.03)
    async def target(index, request):
        await asyncio.sleep(60)
        return httpx.Response(200)
    _, clients = network(target)
    with pytest.raises(RuntimeError, match="近期均失败"):
        with manager.lease(url="https://source.example/"):
            pytest.fail("timed-out nodes cannot carry a business request")
    assert all(client.is_closed for client in clients) and not manager.allocated


def test_parallel_requests_use_different_verified_nodes(manager, network):
    async def target(index, request):
        await asyncio.sleep(0.01)
        return httpx.Response(200)
    network(target)
    barrier = threading.Barrier(2)
    def request(_):
        with manager.lease(url="https://source.example/") as proxy:
            barrier.wait(timeout=2)
            return urlparse(proxy).port
    with ThreadPoolExecutor(2) as pool:
        assert len(set(pool.map(request, range(2)))) == 2
    assert not manager.allocated


def test_authenticated_proxy_tunnel_and_real_target_tls(manager, tmp_path, monkeypatch):
    """No HTTP mocks: tunnel TLS to a local HTTPS server through an auth proxy."""
    import base64
    from datetime import datetime, timedelta, timezone
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import ipaddress
    import select
    import socket
    import ssl
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
                   .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    methods, authentications = [], []
    password = manager._credentials()["password"]
    expected = "Basic " + base64.b64encode(f"cinefold:{password}".encode()).decode()

    class Target(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_HEAD(self):
            methods.append("HEAD")
            self.send_response(204)
            self.end_headers()
        def do_GET(self):
            methods.append("GET")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"actual target response")

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_CONNECT(self):
            valid = self.headers.get("Proxy-Authorization") == expected
            authentications.append(valid)
            if not valid:
                self.send_error(407)
                return
            host, port = self.path.rsplit(":", 1)
            with socket.create_connection((host, int(port)), timeout=2) as upstream:
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True
                while True:
                    ready, _, _ = select.select([self.connection, upstream], [], [], 2)
                    if not ready: return
                    for source in ready:
                        data = source.recv(65536)
                        if not data: return
                        destination = upstream if source is self.connection else self.connection
                        destination.sendall(data)

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    target.socket = context.wrap_socket(target.socket, server_side=True)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    monkeypatch.setattr(mihomo, "FIRST_PORT", proxy.server_port)
    manager.nodes = manager.nodes[:1]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (target, proxy)]
    for thread in threads: thread.start()
    try:
        url = f"https://127.0.0.1:{target.server_port}/movie"
        with manager.lease(url=url) as selected, httpx.Client(proxy=selected, verify=False, trust_env=False, timeout=2) as client:
            assert client.get(url).text == "actual target response"
        assert methods == ["HEAD", "GET"]
        assert authentications == [True, True]
        assert not manager.allocated
    finally:
        for server in (target, proxy):
            server.shutdown()
            server.server_close()
        for thread in threads: thread.join(timeout=2)
