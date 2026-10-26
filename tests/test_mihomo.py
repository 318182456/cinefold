"""Proxy identity, persistence, subscription recovery and verified core updates."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import mihomo
from app.services.mihomo import Mihomo


@pytest.fixture
def manager(tmp_path, monkeypatch):
    value = Mihomo(tmp_path)
    # Existing routing tests isolate health I/O; preflight integration has its
    # own tests with actual asynchronous HTTP responses/cancellation.
    monkeypatch.setattr(value, "_preflight_needed", lambda proxy, url: False)
    monkeypatch.setattr(mihomo, "_instance", value)
    from app.modules.ladysite import base
    monkeypatch.setattr(base, "_BYPASS_RECOVERY", {})
    monkeypatch.setattr(base, "_BYPASS_QUEUES", {})
    yield value
    value.shutdown()


def wait(manager):
    deadline = time.monotonic() + 3
    while manager.state.get("running") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not manager.state.get("running")


def nodes():
    return [{"name": "Japan", "type": "http", "server": "127.0.0.1", "port": 8001},
            {"name": "US", "type": "http", "server": "127.0.0.1", "port": 8002}]


def client(manager, handler, monkeypatch):
    monkeypatch.setattr(manager, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True))


def test_node_selection_has_distinct_identity_on_fixed_port_and_persists(manager):
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="Japan", trawl_host="cinefold", subscription="https://sub.example/secret")
    local = manager.proxy()
    japan = manager.proxy(remote=True)
    manager.config["selected"] = "US"
    us = manager.proxy(remote=True)
    assert "127.0.0.1:17890" in local
    assert "@cinefold:17890" in japan and "@cinefold:17890" in us
    assert japan != us
    runtime = manager._runtime()
    assert runtime["external-controller"] == "127.0.0.1:19090"
    assert len(runtime["listeners"]) == 1
    assert runtime["listeners"][0]["port"] == 17890
    assert len(runtime["listeners"][0]["users"]) == 2
    assert runtime["skip-auth-prefixes"] == []
    assert runtime["rules"][-1] == "MATCH,REJECT"
    for proxy in (japan, us):
        username = httpx.URL(proxy).username
        assert f"IN-USER,{username},{username}" in runtime["rules"]
        assert any(g["name"] == username and g["proxies"] == [manager.proxy_name(proxy)] for g in runtime["proxy-groups"])
    mihomo._write(manager.root / "nodes.json", manager.nodes)
    mihomo._write(manager.root / "settings.json", manager.config)
    restored = Mihomo(manager.root)
    assert restored.proxy(remote=True) == us
    assert restored.status()["subscription"] == "********"
    assert "secret" not in json.dumps(restored.status())


def test_proxy_identity_survives_reordering_and_rejects_replaced_nodes(manager):
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="Japan")
    old = manager.proxy()
    manager.nodes.reverse()
    assert manager.proxy_name(old) == "Japan"
    assert manager._probe_key(old, "https://target.test/")[1] == manager._identity(nodes()[0])
    manager.nodes = [nodes()[1]]
    assert manager.proxy_name(old) == "未知节点"
    manager.note_result("https://target.test/", old, False)
    assert not manager.health
    with pytest.raises(ValueError, match="身份已失效"):
        manager._probe_key(old, "https://target.test/")


def test_fixed_port_rules_handle_punctuation_and_reject_wrong_credentials(manager):
    manager.nodes = [dict(nodes()[0], name="airport / JP, 01")]
    manager.config.update(enabled=True, selected=manager.nodes[0]["name"])
    proxy = manager.proxy()
    runtime = manager._runtime()
    assert len(runtime["listeners"]) == 1
    assert len(runtime["rules"][0].split(',')) == 3
    assert manager._proxy_index(proxy.replace(manager.credentials['password'], 'wrong')) is None
    assert manager._proxy_index(proxy.replace(':17890', ':17891')) is None


def test_disabled_preserves_legacy_proxy_and_no_trawl_payload_proxy(manager, monkeypatch):
    from app.modules.ladysite import base
    assert mihomo.local_proxy("http://old-proxy") == "http://old-proxy"
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "solution": {"response": "page"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        assert base._fetch_bypass_response(transport, "http://trawl/v1", "https://target.example", "", 1000) == "page"
    assert "proxy" not in requests[0]


def test_enabled_trawl_proxy_uses_docker_hostname(manager):
    from app.modules.ladysite import base
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="US")
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "solution": {"response": "page"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        base._fetch_bypass_response(transport, "http://trawl/v1", "https://target.example", "age=1", 1000)
    assert requests[0]["proxy"] == {"url": manager.proxy(remote=True)}
    assert "@cinefold:17890" in requests[0]["proxy"]["url"]
    assert manager.proxy_name(requests[0]["proxy"]["url"]) == "US"
    assert requests[0]["cookies"] == [{"name": "age", "value": "1"}]


def test_direct_source_uses_local_core_but_explicit_source_proxy_wins(manager, monkeypatch):
    from app.modules.ladysite import base
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="Japan")
    original = httpx.Client
    seen = []

    def factory(**kwargs):
        seen.append(kwargs["proxy"])
        return original(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="ok")))

    monkeypatch.setattr(base.httpx, "Client", factory)
    monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(proxy="http://legacy", bypass_url=""))
    source = base.SiteClient("https://source.example", interval=0)
    assert source.get("/movie") == "ok"
    source.proxy = "http://independent"
    assert source.get("/movie") == "ok"
    assert seen == [manager.proxy(), "http://independent"]


def test_subscription_only_imports_nodes_and_keeps_selection(manager, monkeypatch):
    manager.config.update(subscription="https://subscription.example/token", selected="US")
    data = {"proxies": nodes(), "external-controller": "0.0.0.0:9999", "tun": {"enable": True}}
    client(manager, lambda request: httpx.Response(200, json=data), monkeypatch)
    manager._subscription()
    assert manager.config["selected"] == "US"
    assert manager.nodes == nodes()
    restored = Mihomo(manager.root)
    assert restored.nodes == nodes()
    assert restored._runtime()["external-controller"] == "127.0.0.1:19090"
    assert "tun" not in restored._runtime()


@pytest.mark.parametrize("data", ["not yaml", {"proxies": []}, {"proxies": [nodes()[0], nodes()[0]]},
                                      {"proxies": [{"name": "DIRECT", "type": "direct"}]}])
def test_invalid_subscription_preserves_working_nodes(manager, monkeypatch, data):
    manager.nodes = nodes()
    manager.config["subscription"] = "https://subscription.example/token"
    client(manager, lambda request: httpx.Response(200, json=data), monkeypatch)
    with pytest.raises(ValueError):
        manager._subscription()
    assert manager.nodes == nodes()


def test_subscription_failed_restart_restores_old_nodes(manager, monkeypatch):
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="US", subscription="https://subscription.example/token")
    manager.binary.write_bytes(b"old")
    client(manager, lambda request: httpx.Response(200, json={"proxies": [nodes()[0]]}), monkeypatch)
    monkeypatch.setattr(manager, "_validate", lambda **kwargs: None)
    monkeypatch.setattr(manager, "status", lambda: {"running": True})
    starts = []

    def start():
        starts.append(manager.nodes)
        if len(starts) == 1:
            raise RuntimeError("failed")

    monkeypatch.setattr(manager, "_start", start)
    with pytest.raises(RuntimeError):
        manager._subscription()
    assert manager.nodes == nodes() and manager.config["selected"] == "US"
    assert starts[-1] == nodes()


def test_core_health_failure_restores_previous_executable(manager, monkeypatch):
    manager.binary.write_bytes(b"working core")
    manager.version = "old"
    manager.nodes = nodes()
    manager.config["enabled"] = True
    candidate = manager.root / "candidate"
    candidate.write_bytes(b"broken core")
    attempts = []

    def start():
        attempts.append(manager.binary.read_bytes())
        if attempts[-1] == b"broken core":
            raise RuntimeError("health check failed")

    monkeypatch.setattr(manager, "_start", start)
    with pytest.raises(RuntimeError):
        manager._replace(candidate, "new")
    assert attempts == [b"broken core", b"working core"]
    assert manager.binary.read_bytes() == b"working core"
    assert manager.version == "old" and not (manager.root / "installing.json").exists()


def test_restart_recovers_interrupted_install_and_reports_interruption(manager):
    manager.binary.write_bytes(b"incomplete")
    (manager.root / "previous").write_bytes(b"working")
    mihomo._write(manager.root / "installing.json", {"version": "old"})
    mihomo._write(manager.root / "task.json", {"running": True})
    restored = Mihomo(manager.root)
    assert restored.binary.read_bytes() == b"working"
    assert restored.version == "old" and not restored.status()["task"]["running"]
    assert restored.status()["task"]["stage"] == "interrupted"


def test_bad_download_digest_never_replaces_core(manager, monkeypatch):
    manager.binary.write_bytes(b"working")
    manager.version = "old"
    release = {"url": "https://github.com/asset.gz", "version": "new", "name": "asset.gz", "sha256": "0" * 64}
    monkeypatch.setattr(mihomo, "discover_release", lambda client: release)
    client(manager, lambda request: httpx.Response(200, content=gzip.compress(b"broken")), monkeypatch)
    with pytest.raises(ValueError, match="SHA256"):
        manager._install()
    assert manager.binary.read_bytes() == b"working" and manager.version == "old"
    assert not (manager.root / "download.gz").exists()


def test_official_asset_identity_and_digest_are_checked(monkeypatch):
    monkeypatch.setattr(mihomo, "release_asset", lambda: ("mihomo-linux-arm64", ".gz"))
    name = "mihomo-linux-arm64-v1.2.3.gz"
    digest = "a" * 64

    def handler(request):
        if request.url.path.endswith("latest"):
            return httpx.Response(302, headers={"location": f"{mihomo.RELEASE_ROOT}/tag/v1.2.3"})
        return httpx.Response(200, text=f'<clipboard-copy aria-label="Copy to clipboard digest for {name}" value="sha256:{digest}"></clipboard-copy>')

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as c:
        release = mihomo.discover_release(c)
    assert release["sha256"] == digest
    assert release["url"] == f"{mihomo.RELEASE_ROOT}/download/v1.2.3/{name}"


def test_masked_subscription_preserved_and_duplicate_operations_rejected(manager, monkeypatch):
    import threading
    manager.config["subscription"] = "https://sub.example/secret"
    started, release = threading.Event(), threading.Event()

    def start():
        started.set()
        release.wait(2)

    manager.binary.write_bytes(b"core")
    manager.nodes = nodes()
    monkeypatch.setattr(manager, "_start", start)
    manager.submit("apply", {"enabled": True, "selected": "Japan", "subscription": "********"})
    try:
        assert started.wait(1)
        with pytest.raises(ValueError, match="正在"):
            manager.submit("install")
    finally:
        release.set()
    wait(manager)
    assert manager.config["subscription"] == "https://sub.example/secret"
    assert manager.state["stage"] == "done"
    assert Mihomo(manager.root).config["selected"] == "Japan"


def test_management_endpoints_require_authentication_and_validate_node(manager):
    from app.api.endpoints import get_current_user, mihomo as endpoint
    app = FastAPI()
    app.include_router(endpoint.router)
    with TestClient(app) as c:
        assert c.get("/mihomo").status_code == 401
        app.dependency_overrides[get_current_user] = lambda: "test-admin"
        assert c.get("/mihomo").json()["data"]["installed"] is False
        response = c.post("/mihomo/config", json={"enabled": True, "selected": "missing"})
        assert response.json()["code"] == 400
        assert c.post("/mihomo/config", json={"trawl_host": "http://localhost:123"}).status_code == 422
        assert c.post("/mihomo/action/not-real").json()["code"] == 400


@pytest.mark.parametrize("system,machine,expected", [("Linux", "x86_64", "mihomo-linux-amd64-compatible"),
    ("Linux", "aarch64", "mihomo-linux-arm64"), ("Windows", "AMD64", "mihomo-windows-amd64-compatible")])
def test_platform_asset_selection(system, machine, expected):
    assert mihomo.release_asset(system, machine)[0] == expected


def test_per_source_preference_and_success_are_isolated(manager):
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="Japan", source_nodes={"dmm": "US"},
                          source_hosts={"dmm": "dmm.co.jp"})
    dmm = manager.proxy(url="https://www.dmm.co.jp/detail", remote=True)
    javdb = manager.proxy(url="https://javdb.com/movie", remote=True)
    assert manager.proxy_name(dmm) == "US" and manager.proxy_name(javdb) == "Japan"
    manager.note_result("https://javdb.com/movie", javdb, False)
    fallback = manager.proxy(url="https://javdb.com/movie", remote=True)
    assert manager.proxy_name(fallback) == "US"
    manager.note_result("https://javdb.com/movie", fallback, True)
    assert manager.proxy(url="https://javdb.com/other", remote=True) == fallback
    assert ":17890" in manager.proxy(url="https://other.example", remote=True)
    assert manager.config["selected"] == "Japan"
    manager.config["automatic"] = False
    assert manager.proxy(url="https://javdb.com/movie", remote=True) == javdb


def test_trawl_automatically_changes_exit_and_reuses_success(manager):
    from app.modules.ladysite import base
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="Japan")
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if manager.proxy_name(body["proxy"]["url"]) == "Japan":
            return httpx.Response(500, json={"status": "error", "message": "Tier 3 failed (goto: Timeout 30000ms exceeded.)"})
        return httpx.Response(200, json={"status": "ok", "solution": {"status": 200, "response": "good"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as c:
        assert base._fetch_bypass_response(c, "http://trawl/v1", "https://javdb.com/v/123", "", 90000) == "good"
        assert base._fetch_bypass_response(c, "http://trawl/v1", "https://javdb.com/v/456", "", 90000) == "good"
    assert len(requests) == 3
    assert manager.proxy_name(requests[0]["proxy"]["url"]) == "Japan"
    assert all(manager.proxy_name(request["proxy"]["url"]) == "US" for request in requests[1:])
    assert requests[0]["maxTimeout"] <= 45000


def test_trawl_missing_work_does_not_rotate_nodes(manager):
    from app.modules.ladysite import base
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="Japan")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "solution": {"status": 404, "response": "not found"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as c:
        assert base._fetch_bypass_response(c, "http://trawl/v1", "https://javdb.com/missing", "", 90000) == ""
    assert len(requests) == 1 and not manager.health


def test_trawl_disconnect_does_not_blame_exit_nodes(manager):
    from app.modules.ladysite import base
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="Japan")
    requests = []

    def respond(request):
        requests.append(request)
        raise httpx.RemoteProtocolError("service disconnected")

    with httpx.Client(transport=httpx.MockTransport(respond)) as c:
        with pytest.raises(httpx.RemoteProtocolError):
            base._fetch_bypass_response(c, "http://trawl/v1", "https://javdb.com/movie", "", 90000)
    assert len(requests) == 1 and not manager.health


def test_failed_nodes_cool_down_then_are_available_again(manager, monkeypatch):
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="Japan")
    for _ in range(2):
        proxy = manager.proxy(url="https://javdb.com/", remote=True)
        manager.note_result("https://javdb.com/", proxy, False)
    with pytest.raises(RuntimeError, match="近期均失败"):
        manager.proxy(url="https://javdb.com/", remote=True)
    assert manager.proxy(url="https://other.example/", remote=True)
    manager.health = {key: stamp - 121 for key, stamp in manager.health.items()}
    assert ":17890" in manager.proxy(url="https://javdb.com/", remote=True)


def test_direct_transport_failure_switches_node_for_retry(manager, monkeypatch):
    from app.modules.ladysite import base
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="Japan")
    original = httpx.Client
    proxies = []

    def factory(**kwargs):
        proxy = kwargs["proxy"]
        proxies.append(proxy)

        def respond(request):
            if manager.proxy_name(proxy) == "Japan":
                raise httpx.ConnectError("node unreachable")
            return httpx.Response(200, text="metadata")

        return original(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(base.httpx, "Client", factory)
    monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(proxy="", bypass_url=""))
    site = base.SiteClient("https://javdb.com", interval=0)
    assert site.get("/movie") == "metadata"
    assert len(proxies) == 2 and proxies[0] != proxies[1]


def test_source_bindings_saved_through_api_and_restored(manager, monkeypatch):
    from app.api.endpoints import get_current_user, mihomo as endpoint
    from app.modules.ladysite import sources
    manager.nodes = nodes()
    mihomo._write(manager.root / "nodes.json", manager.nodes)
    monkeypatch.setattr(sources, "get_source", lambda key: {"host": "https://www.dmm.co.jp"} if key == "dmm" else None)
    app = FastAPI()
    app.include_router(endpoint.router)
    app.dependency_overrides[get_current_user] = lambda: "admin"
    with TestClient(app) as c:
        response = c.post("/mihomo/config", json={"source_nodes": {"dmm": "US"}, "selected": "Japan",
                         "groups": {"US group": ["US"]}, "source_groups": {"dmm": "US group"}})
        assert response.json()["code"] == 200
    wait(manager)
    restored = Mihomo(manager.root)
    assert restored.config["source_nodes"] == {"dmm": "US"}
    assert restored.config["groups"] == {"US group": ["US"]}
    assert restored.config["source_groups"] == {"dmm": "US group"}
    restored.config["enabled"] = True
    assert restored.proxy_name(restored.proxy(url="https://www.dmm.co.jp/", remote=True)) == "US"


def test_nested_subscriptions_merge_names_and_ignore_upstream_groups(manager, monkeypatch):
    docs = {
        "/root": {"proxies": [nodes()[0]], "proxy-groups": [{"name": "untrusted", "type": "select", "proxies": ["Japan"]}],
                  "proxy-providers": {"airportA": {"type": "http", "url": "/a"},
                                      "airportB": {"type": "http", "url": "/b"}}},
        "/a": {"proxies": [nodes()[0]]},
        "/b": {"proxy-providers": {"nested": {"type": "inline", "payload": [nodes()[1]]}}},
    }
    manager.config["subscription"] = "https://subs.example/root"
    client(manager, lambda request: httpx.Response(200, json=docs[request.url.path]), monkeypatch)
    manager._subscription()
    assert {node["name"] for node in manager.nodes} == {"Japan", "airportA / Japan", "airportB / nested / US"}
    assert all(group["name"] != "untrusted" for group in manager._runtime()["proxy-groups"])
    assert all(report["status"] == "ok" for report in manager.providers)


def test_child_subscription_failure_reuses_only_its_previous_cache(manager, monkeypatch):
    manager.config["subscription"] = "https://subs.example/root"
    fail = False

    def respond(request):
        if request.url.path == "/root":
            return httpx.Response(200, json={"proxies": [nodes()[0]], "proxy-providers": {"airport": {"type": "http", "url": "/child"}}})
        return httpx.Response(503) if fail else httpx.Response(200, json={"proxies": [nodes()[1]]})

    client(manager, respond, monkeypatch)
    manager._subscription()
    saved = list(manager.nodes)
    fail = True
    manager._subscription()
    assert manager.nodes == saved
    assert any(report["status"] == "cached" and report["count"] == 1 for report in manager.providers)


def test_subscription_cycle_does_not_recurse_forever(manager, monkeypatch):
    manager.config["subscription"] = "https://subs.example/root"
    client(manager, lambda request: httpx.Response(200, json={"proxy-providers": {"loop": {"type": "http", "url": "/root"}}}), monkeypatch)
    with pytest.raises(ValueError, match="没有可导入"):
        manager._subscription()


def test_custom_group_limits_automatic_fallback(manager):
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="Japan", groups={"US only": ["US"]},
                          source_groups={"dmm": "US only"}, source_hosts={"dmm": "www.dmm.co.jp"})
    proxy = manager.proxy(url="https://www.dmm.co.jp/", remote=True)
    assert manager.proxy_name(proxy) == "US"
    manager.note_result("https://www.dmm.co.jp/", proxy, False)
    with pytest.raises(RuntimeError):
        manager.proxy(url="https://www.dmm.co.jp/", remote=True)
    assert ":17890" in manager.proxy(url="https://other.example/", remote=True)
    runtime_groups = manager._runtime()["proxy-groups"]
    assert any(group["proxies"] == ["US"] for group in runtime_groups)


def test_parallel_trawl_requests_reserve_distinct_nodes(manager, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from app.modules.ladysite import base
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="Japan")
    barrier = threading.Barrier(2)
    proxies = []

    def respond(request):
        proxies.append(json.loads(request.content)["proxy"]["url"])
        barrier.wait(timeout=2)
        return httpx.Response(200, json={"status": "ok", "solution": {"status": 200, "response": "good"}})

    original = httpx.Client
    monkeypatch.setattr(base, "http_client", lambda **kw: original(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(base, "get_settings", lambda: SimpleNamespace(bypass_url="http://trawl/v1"))
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda i: base.fetch_via_bypass(f"https://site{i}.example/"), range(2)))
    assert results == ["good", "good"] and len(set(proxies)) == 2
    assert not manager.allocated


def test_busy_node_pool_queues_until_release(manager):
    from concurrent.futures import ThreadPoolExecutor
    manager.nodes = [nodes()[0]]
    manager.config.update(enabled=True, selected="Japan")

    def second():
        with manager.lease(url="https://second.example/", timeout=0.1):
            pytest.fail("busy node must not be shared")

    with ThreadPoolExecutor(1) as pool, manager.lease(url="https://first.example/"):
        with pytest.raises(TimeoutError):
            pool.submit(second).result(timeout=1)
    assert not manager.allocated
    with manager.lease(url="https://first.example/") as proxy:
        assert proxy


def test_duplicate_node_aliases_cannot_bypass_concurrency_reservation(manager):
    from concurrent.futures import ThreadPoolExecutor
    manager.nodes = [nodes()[0], {**nodes()[0], "name": "alias"}]
    manager.config.update(enabled=True, selected="Japan")

    def second():
        with manager.lease(url="https://second.example/", timeout=0.1):
            pytest.fail("same upstream with a different alias is still busy")

    with ThreadPoolExecutor(1) as pool, manager.lease(url="https://first.example/"):
        with pytest.raises(TimeoutError):
            pool.submit(second).result(timeout=1)


def test_short_sequential_burst_moves_to_another_node(manager):
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="Japan")
    proxies = []
    for _ in range(3):
        with manager.lease(url="https://site.example/") as proxy:
            proxies.append(proxy)
    assert proxies[0] == proxies[1]
    assert proxies[2] != proxies[0]


def test_single_node_pool_does_not_ignore_burst_limit(manager):
    manager.nodes = [nodes()[0]]
    manager.config.update(enabled=True, selected="Japan")
    for _ in range(2):
        with manager.lease(url="https://site.example/"):
            pass
    with pytest.raises(TimeoutError):
        with manager.lease(url="https://site.example/", timeout=0.05):
            pytest.fail("third request must queue")
    assert not manager.allocated


def test_subscription_activation_waits_for_current_requests(manager):
    from concurrent.futures import ThreadPoolExecutor
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="Japan")
    with ThreadPoolExecutor(1) as pool:
        with manager.lease(url="https://site.example/"):
            future = pool.submit(manager._activate_nodes, [nodes()[1]])
            deadline = time.monotonic() + 1
            while not manager.draining and time.monotonic() < deadline:
                time.sleep(0.01)
            assert manager.draining and not future.done()
            assert manager.nodes == nodes()
        future.result(timeout=1)
    assert manager.nodes == [nodes()[1]] and not manager.draining


def test_default_groups_rebuild_members_without_changing_source_binding(manager):
    manager.nodes = [dict(nodes()[0], name="airport / JP-01"), dict(nodes()[1], name="US-01")]
    manager.config.update(enabled=True, selected="airport / JP-01", source_groups={"dmm": "日本节点"})
    assert manager.status()["default_groups"]["日本节点"] == ["airport / JP-01"]
    manager._activate_nodes([dict(nodes()[1], name="new subscription / 🇯🇵 Tokyo"), dict(nodes()[0], name="HK-02")])
    assert manager.config["source_groups"] == {"dmm": "日本节点"}
    assert manager.status()["default_groups"]["日本节点"] == ["new subscription / 🇯🇵 Tokyo"]
    assert ":17890" in manager.proxy(source_key="dmm", url="https://www.dmm.co.jp/")
    assert not manager.config["groups"]  # no static member list was saved


def test_default_rules_handle_country_flags_and_ignore_direct_and_provider_names():
    from app.services.mihomogroups import build_groups
    data = [{"name": name, "type": kind} for name, kind in [
        ("🇯🇵 Tokyo", "ss"), ("HK-02", "http"), ("TW 3", "ss"), ("Singapore", "ss"),
        ("US 5", "ss"), ("Japan airport / Germany", "ss"), ("直连", "direct")]]
    groups = build_groups(data)
    assert groups["日本节点"] == ["🇯🇵 Tokyo"]
    assert groups["香港节点"] == ["HK-02"]
    assert groups["其他节点"] == ["Japan airport / Germany"]
    assert len(groups["自动选择"]) == 6


def test_official_sources_get_default_japan_pool_with_safe_empty_region_fallback(manager):
    manager.nodes = [dict(nodes()[0], name="Japan"), dict(nodes()[1], name="US")]
    manager.config.update(enabled=True, selected="US")
    assert ":17890" in manager.proxy(source_key="dmm", url="https://www.dmm.co.jp/")
    assert ":17890" in manager.proxy(source_key="brand:s1", url="https://s1s1s1.com/")
    manager.nodes = [dict(nodes()[1], name="US")]
    manager.config["selected"] = "US"
    assert manager.status()["source_defaults"]["dmm"] == "自动选择"
    assert manager.proxy(source_key="dmm", url="https://www.dmm.co.jp/")


def test_saving_replacement_subscription_refreshes_nodes_and_keeps_group_rule(manager, monkeypatch):
    manager.nodes = nodes()
    manager.binary.write_bytes(b"core")
    manager.config.update(enabled=True, selected="Japan", subscription="https://old.example/sub",
                          source_groups={"dmm": "日本节点"})
    calls = []
    monkeypatch.setattr(manager, "_start", lambda: None)

    def refresh():
        calls.append(manager.config["subscription"])
        manager._activate_nodes([dict(nodes()[0], name="JP-new")])

    monkeypatch.setattr(manager, "_subscription", refresh)
    manager.submit("apply", {"subscription": "https://new.example/sub"})
    wait(manager)
    assert calls == ["https://new.example/sub"]
    assert manager.config["selected"] == "JP-new"
    assert manager.config["source_groups"] == {"dmm": "日本节点"}
    assert Mihomo(manager.root).status()["default_groups"]["日本节点"] == ["JP-new"]


def test_failed_site_requeues_behind_waiting_site_between_proxy_attempts(manager, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from app.modules.ladysite import base
    from app.modules.ladysite.transport import BrowserQueue
    from tests.test_bypass_queue import queued

    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="Japan")
    gate = BrowserQueue(1)
    monkeypatch.setattr(base, "_BYPASS_QUEUES", {"http://trawl/v1": gate})
    slow_entered, release_slow = threading.Event(), threading.Event()
    order = []

    def respond(request):
        host = httpx.URL(json.loads(request.content)["url"]).host
        order.append(host)
        if len(order) == 1:
            slow_entered.set()
            assert release_slow.wait(2)
            return httpx.Response(500, json={"status": "error", "message": "goto: Timeout 30000ms exceeded."})
        return httpx.Response(200, json={"status": "ok", "solution": {"status": 200, "response": "good"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as c, ThreadPoolExecutor(2) as pool:
        slow = pool.submit(base._fetch_bypass_response, c, "http://trawl/v1", "https://slow.example/", "", 90000)
        try:
            assert slow_entered.wait(1)
            healthy = pool.submit(base._fetch_bypass_response, c, "http://trawl/v1", "https://healthy.example/", "", 90000)
            queued(gate, 1)
        finally:
            release_slow.set()
        assert slow.result(timeout=3) == healthy.result(timeout=3) == "good"
    assert order == ["slow.example", "healthy.example", "slow.example"]
    assert gate.active == 0 and not manager.allocated


def test_quick_check_gives_one_node_the_full_short_solve_window(manager):
    from app.modules.ladysite import base
    manager.nodes = nodes()
    manager.config.update(enabled=True, selected="Japan")
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "solution": {"status": 200, "response": "good"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as c:
        assert base._fetch_bypass_response(c, "http://trawl/v1", "https://check.example/", "", 20000, quick=True) == "good"
    assert len(requests) == 1 and 19000 <= requests[0]["maxTimeout"] <= 20000


def test_trawl_challenge_gets_more_than_thirty_seconds_with_many_nodes(manager):
    from app.modules.ladysite import base
    manager.nodes = [*nodes(), dict(nodes()[1], name="Third", port=8003)]
    manager.config.update(enabled=True, selected="Japan")
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        # Simulate a challenge that needs 35 seconds; three 30s jobs all fail.
        if body["maxTimeout"] < 35000:
            return httpx.Response(500, json={"status": "error", "message": "Scrape deadline exceeded"})
        return httpx.Response(200, json={"status": "ok", "solution": {"status": 200, "response": "rank page"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as c:
        assert base._fetch_bypass_response(c, "http://trawl/v1", "https://javdb.com/rankings/movies", "", 90000) == "rank page"
    assert len(requests) == 1 and 40000 <= requests[0]["maxTimeout"] <= 45000


def test_failed_exit_aliases_share_cooldown_and_clear_cached_alias(manager):
    manager.nodes = [nodes()[0], dict(nodes()[0], name="other-subscription / alias"), nodes()[1]]
    manager.config.update(enabled=True, selected="Japan")
    target = "https://javdb.com/rankings/movies"
    first = manager.proxy(url=target)
    manager.config["selected"] = "other-subscription / alias"
    alias = manager.proxy(url=target)
    manager.note_result(target, alias, True)
    manager.note_result(target, first, False)
    assert "javdb.com" not in manager.winners
    assert manager.proxy_name(manager.proxy(url=target)) == "US"
    assert manager.proxy_name(first) == "Japan"
    assert manager.proxy_name(manager.proxy(url="https://other.example/")) == "other-subscription / alias"
