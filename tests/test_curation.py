"""跨模块集成约束：订阅不重置、取消不复活、Emby 原子快照与硬链接查重。"""
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import DBBase
from app.database.models import (
    Code, CodeProfile, DownloadProfile, EmbyWatch,
    MediaLink, RuleExclusion, SubscriptionRule,
)


@pytest.fixture
def db(monkeypatch):
    import app.database.session as database
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    DBBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    yield factory
    engine.dispose()


@pytest.fixture
def client(db):
    from app.api import create_app
    from app.api.endpoints import get_current_user
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: "test"
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client


@pytest.fixture(autouse=True)
def no_startup(monkeypatch):
    import app.api
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def lifespan(app):
        yield
    monkeypatch.setattr(app.api, "lifespan", lifespan)


def test_rule_matches_exact_metadata_and_unknown_casts():
    from app.services.rules import matches
    item = {"code": "SONE-123", "genres": '["标签一", "标签二"]', "casts": "演员甲,演员乙", "publisher": "ABC"}
    assert matches(item, {"prefixes": ["sone"], "genres": ["标签一"], "publisher": "abc", "max_coactors": 2})
    assert not matches(item, {"casts": ["演员"]})
    assert not matches(item, {"prefixes": ["SON"]})
    assert not matches({"code": "SONE-123"}, {"max_coactors": 2})
    assert not matches(item, {"earliest_release_date": "2026-01-01"})


def test_rules_assign_profile_once_and_preserve_states(db, monkeypatch):
    from app.services.rules import run_rules
    monkeypatch.setattr("app.services.is_filtered_code", lambda *a: "")
    with db.begin() as session:
        profile = DownloadProfile(name="中字", filters={"only_chinese": True}, sort="seeders")
        session.add(profile); session.flush()
        session.add_all([SubscriptionRule(name="前缀", enabled=True, conditions={"prefixes": ["SONE"]}, profile_id=profile.id),
            Code(code="SONE-101", status=0), Code(code="SONE-102", status=2), Code(code="SONE-103", status=4),
            Code(code="SONE-104", status=0), RuleExclusion(code="SONE-104")])
    assert run_rules() == 1
    assert run_rules() == 0
    with db() as session:
        assert session.get(Code, "SONE-102").status == 2
        assert session.get(Code, "SONE-103").status == 4
        assert session.get(Code, "SONE-104").status == 0
        assert session.get(CodeProfile, "SONE-101").profile_id == profile.id


def test_download_profile_affects_automatic_search_only(db, monkeypatch):
    from app import services
    from app.schemas.torrent import Torrent
    monkeypatch.setattr(services, "_search_pt_cached", lambda *a, **k: [Torrent(title="普通", chinese=False), Torrent(title="中字", chinese=True)])
    monkeypatch.setattr(services, "build_site_priority", lambda: [])
    with db.begin() as session:
        p = DownloadProfile(name="中字", filters={"only_chinese": True}, sort="")
        session.add(p); session.flush(); session.add(CodeProfile(code="SONE-101", profile_id=p.id))
    assert len(services.search_torrents("SONE-101")) == 1
    assert len(services.search_torrents("SONE-101", use_filter=False)) == 2


def test_profile_api_rejects_invalid_and_in_use_delete(client, db):
    assert client.post("/api/v1/profiles", json={"name": "bad", "sort": "unknown"}).status_code == 422
    assert client.post("/api/v1/profiles", json={"name": "bad", "filters": {"min_size": "10GB", "max_size": "2GB"}}).status_code == 422
    response = client.post("/api/v1/profiles", json={"name": "profile"}).json()
    profile_id = response["data"]["id"]
    with db.begin() as session:
        session.add(CodeProfile(code="SONE-001", profile_id=profile_id))
    assert client.delete(f"/api/v1/profiles/{profile_id}").status_code == 409


def test_rule_preview_never_subscribes(client, db):
    with db.begin() as session:
        session.add(Code(code="SONE-001", status=0))
    response = client.post("/api/v1/subscription-rules/preview", json={"name": "test", "conditions": {"prefixes": "SONE"}})
    assert response.json()["data"]["total"] == 1
    with db() as session:
        assert session.get(Code, "SONE-001").status == 0
    assert client.post("/api/v1/subscription-rules", json={"name": "empty", "enabled": True}).status_code == 422


def test_smart_collection_and_preferences(client, db):
    with db.begin() as session:
        session.add_all([Code(code="SONE-001", status=4, genres="标签甲"), Code(code="SONE-002", status=4, genres="标签乙"), Code(code="SONE-003", status=0, genres="标签甲")])
    result = client.post("/api/v1/collections", json={"name": "智能", "kind": "smart", "conditions": {"genres": "标签甲"}}).json()["data"]
    response = client.get("/api/v1/library", params={"collection_id": result["id"]}).json()["data"]
    assert [x["code"] for x in response["items"]] == ["SONE-001"]
    assert client.get("/api/v1/library", params={"collection_id": result["id"], "genre": "标签乙"}).json()["data"]["total"] == 0
    assert client.post(f"/api/v1/collections/{result['id']}/items", json={"codes": ["SONE-002"]}).status_code == 422
    client.patch("/api/v1/library/SONE-001/preference", json={"favorite": True, "notes": "备注", "rating": 8})
    response = client.get("/api/v1/library", params={"favorite": True}).json()["data"]
    assert response["items"][0]["preference"]["notes"] == "备注"
    client.patch("/api/v1/library/SONE-001/preference", json={"rating": None})
    assert client.get("/api/v1/library", params={"favorite": True}).json()["data"]["items"][0]["preference"]["rating"] is None


def test_analysis_filters_use_exact_actor_genre_and_publisher(client, db):
    with db.begin() as session:
        session.add_all([
            Code(code="ABS-101", status=4, casts="演员甲,演员乙", genres="标签甲", publisher="厂牌甲"),
            Code(code="ABS-102", status=4, casts="演员甲乙", genres="标签甲乙", publisher="厂牌甲乙"),
            Code(code="ABS-103", status=0, casts="演员甲", genres="标签甲", publisher="厂牌甲"),
        ])
    for field, value in [("actor", "演员甲"), ("genre", "标签甲"), ("publisher", "厂牌甲")]:
        result = client.get('/api/v1/library', params={field: value}).json()['data']
        assert [item['code'] for item in result['items']] == ['ABS-101']


def test_resource_index_routes_and_schedule_are_removed(client):
    from app.scheduler import INTERVAL_JOBS
    assert 'collect_resources' not in INTERVAL_JOBS
    assert client.get('/api/v1/resources').status_code == 404
    assert client.post('/api/v1/resources/collect').status_code == 404


def test_health_does_not_count_hardlinks_as_duplicate(db, tmp_path):
    from app.services.libraryhealth import scan
    original = tmp_path / "SONE-001-CD1.mp4"; original.write_bytes(b"test content")
    linked = tmp_path / "linked.mp4"
    try:
        os.link(original, linked)
    except OSError:
        pytest.skip("filesystem does not support hardlinks")
    duplicate = tmp_path / "duplicate.mp4"; duplicate.write_bytes(original.read_bytes())
    with db.begin() as session:
        session.add(Code(code="SONE-001", status=4))
        for path in (original, linked, duplicate):
            session.add(MediaLink(code="SONE-001", link_path=str(path), source_path=str(path)))
    result = scan(hashes=True)
    assert result["physical_files"] == 2
    assert len(result["exact"]) == 1
    assert len(result["exact"][0]["files"]) == 2
    assert result["duplicate_bytes"] == len(b"test content")
    assert result["versions"][0]["multipart"]
    assert original.exists() and linked.exists() and duplicate.exists()


def test_emby_failed_pagination_preserves_snapshot(db, monkeypatch):
    from app.core.config import Settings
    from app.services import embysync
    monkeypatch.setattr(embysync, "get_settings", lambda: Settings(emby_url="http://emby", emby_api_key="secret", emby_user_id="u1"))
    with db.begin() as session:
        session.add_all([Code(code="SONE-001", status=4), EmbyWatch(user_id="u1", item_id="old", code="SONE-001", played=True)])
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"Items": [{"Id": "new", "Name": "SONE-001", "UserData": {}}], "TotalRecordCount": 2})
        return httpx.Response(503)
    original_client = httpx.Client
    monkeypatch.setattr(embysync.httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(handler)))
    with pytest.raises(httpx.HTTPStatusError):
        embysync.sync()
    with db() as session:
        assert session.get(EmbyWatch, ("u1", "old")).played


def test_mcp_origin_protocol_and_proposal_only(client, db):
    url = "/api/v1/mcp"
    assert client.post(url, headers={"Origin": "https://untrusted.test"}, json={}).status_code == 403
    assert client.post(url, json=[]).status_code == 400
    init = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}).json()
    assert init["result"]["protocolVersion"] == "2025-11-25"
    assert client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    response = client.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "propose_subscription", "arguments": {"action": "subscribe_codes", "codes": ["SONE-001"]}}}).json()
    assert not response["result"]["isError"]
    with db() as session:
        assert session.get(Code, "SONE-001") is None
    proposal = __import__('json').loads(response["result"]["content"][0]["text"])["proposal"]
    assert proposal["action"] == "subscribe_codes"
    client.post("/api/v1/agent/cancel", json={"proposal_id": proposal["id"]})


def test_new_endpoints_require_auth(db):
    from app.api import create_app
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/library").status_code == 401
        assert client.get("/api/v1/profiles").status_code == 401
        assert client.post("/api/v1/mcp", json={}).status_code == 401
        assert client.post("/api/v1/cloudnas/offline", json={"magnet": "x"}).status_code == 401


def test_emby_success_replaces_user_and_preserves_subscription(db, monkeypatch):
    from app.core.config import Settings
    from app.services import embysync
    monkeypatch.setattr(embysync, "get_settings", lambda: Settings(emby_url="http://emby", emby_api_key="secret", emby_user_id="new"))
    with db.begin() as session:
        session.add_all([Code(code="SONE-001", status=0), EmbyWatch(user_id="old", item_id="old", code="SONE-001", played=True)])
    def handler(request):
        assert request.headers["X-Emby-Token"] == "secret"
        return httpx.Response(200, json={"Items": [{"Id": "1", "Name": "SONE-001", "RunTimeTicks": 72000000000,
            "UserData": {"Played": True, "PlayCount": 3, "PlaybackPositionTicks": 12345678900}}], "TotalRecordCount": 1})
    original_client = httpx.Client
    monkeypatch.setattr(embysync.httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(handler)))
    assert embysync.sync()["matched"] == 1
    with db() as session:
        assert session.get(EmbyWatch, ("old", "old")) is None
        assert session.get(EmbyWatch, ("new", "1")).position_ticks == 12345678900
        assert session.get(Code, "SONE-001").status == 0


def test_cancelled_code_stays_excluded_until_manual_subscription(db, monkeypatch):
    from app import services
    from app.services.rules import run_rules
    monkeypatch.setattr(services, "is_filtered_code", lambda *a: "")
    monkeypatch.setattr(services, "_drop_vanished_history", lambda *a: None)
    monkeypatch.setattr(services, "send_subscribe_message", lambda *a: None)
    with db.begin() as session:
        session.add_all([Code(code="SONE-001", status=1), SubscriptionRule(name="all SONE", enabled=True, conditions={"prefixes": ["SONE"]})])
    assert services.cancel_subscribe("SONE-001")
    assert run_rules() == 0
    assert services.subscribe_code("SONE-001")
    with db() as session:
        assert session.get(RuleExclusion, "SONE-001") is None


def test_cloudnas_uses_timeout_auth_and_closes_channel(monkeypatch):
    from types import SimpleNamespace
    import grpc
    from app.core.config import Settings
    from app.modules import cloudnas
    from app.modules.cloudnas import clouddrive_pb2 as pb
    monkeypatch.setattr(cloudnas, "get_settings", lambda: Settings(cloudnas_url="http://cloud:19798", cloudnas_username="u", cloudnas_password="p", cloudnas_savepath="/115open"))
    calls = []
    class Channel:
        closed = False
        def unary_unary(self, method, **kwargs):
            def invoke(request, **options):
                calls.append((method, request, options))
                if method.endswith("GetToken"):
                    return pb.JWTToken(success=True, token="session-token")
                return pb.FileOperationResult(success=True)
            return invoke
        def close(self):
            self.closed = True
    channel = Channel()
    monkeypatch.setattr(grpc, "insecure_channel", lambda *a, **kw: channel)
    assert cloudnas.add_offline("magnet:?xt=urn:btih:" + "a" * 40)["accepted"]
    assert channel.closed
    assert calls[1][2]["metadata"] == [("authorization", "Bearer session-token")]
    assert calls[1][2]["timeout"] == 30
    assert calls[1][1].toFolder == "/115open"


def test_discord_payload_blocks_mentions_and_handles_html(monkeypatch):
    from app.core.config import Settings
    from app.modules.notify import discord
    monkeypatch.setattr(discord, "get_settings", lambda: Settings(discord_webhook_url="https://discord.test/token"))
    sent = []
    def handler(request):
        sent.append(__import__('json').loads(request.content))
        return httpx.Response(204)
    original_client = httpx.Client
    monkeypatch.setattr(discord.httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(handler)))
    assert discord.DiscordNotifier().send_text_message("<b>hello</b> @everyone")
    assert sent[0]["content"] == "hello @everyone"
    assert sent[0]["allowed_mentions"] == {"parse": []}
