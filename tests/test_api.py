"""API 层测试：鉴权、端点契约、业务流转。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from app.api import create_app
    from app.database.base import DBBase
    from app.database.session import engine

    DBBase.metadata.create_all(engine)
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def token(client):
    """用初始 admin 账号换取 JWT。"""
    from app.database.models import User
    from app.database.session import session_scope
    from app.database.utils.setup import hash_password

    with session_scope() as session:
        user = session.get(User, "admin")
        if user is None:
            session.add(User(username="admin", password=hash_password("testpass")))
        else:
            user.password = hash_password("testpass")

    response = client.post("/api/v1/login", json={"username": "admin", "password": "testpass"})
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200, body
    return body["data"]["token"]


@pytest.fixture
def auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestHealth:
    def test_health_no_auth(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["code"] == 200


class TestAuth:
    def test_login_wrong_password(self, client):
        response = client.post(
            "/api/v1/login", json={"username": "admin", "password": "wrong"}
        )
        assert response.json()["code"] == 401

    def test_login_unknown_user(self, client):
        response = client.post(
            "/api/v1/login", json={"username": "ghost", "password": "x"}
        )
        assert response.json()["code"] == 401

    def test_protected_endpoint_requires_token(self, client):
        assert client.get("/api/v1/dashboard").status_code == 401

    def test_protected_endpoint_with_token(self, client, auth):
        response = client.get("/api/v1/dashboard", headers=auth)
        assert response.status_code == 200
        assert response.json()["code"] == 200

    def test_invalid_token_rejected(self, client):
        response = client.get(
            "/api/v1/dashboard", headers={"Authorization": "Bearer garbage"}
        )
        assert response.status_code == 401

    def test_long_lived_token_works(self, client, auth):
        """长期 token 应能替代 JWT 使用。"""
        issued = client.get("/api/v1/user/token", headers=auth).json()["data"]["token"]
        response = client.get(
            "/api/v1/dashboard", headers={"Authorization": f"Bearer {issued}"}
        )
        assert response.json()["code"] == 200

    def test_query_token_accepted(self, client, auth):
        """图片代理等场景需要 query token。"""
        issued = client.get("/api/v1/user/token", headers=auth).json()["data"]["token"]
        assert client.get(f"/api/v1/dashboard?token={issued}").json()["code"] == 200


class TestDashboard:
    def test_shape(self, client, auth):
        data = client.get("/api/v1/dashboard", headers=auth).json()["data"]
        for key in ("total", "subscribed", "downloading", "downloaded", "actors"):
            assert key in data
            assert isinstance(data[key], int)


class TestConfig:
    def test_get_config_masks_secrets(self, client, auth):
        from app.core.config import get_settings, save_settings

        save_settings({"QBITTORRENT_PASSWORD": "supersecret"})
        get_settings(reload=True)

        data = client.get("/api/v1/config", headers=auth).json()["data"]
        assert data["qbittorrent_password"] == "*" * 8

    def test_save_config_skips_masked_values(self, client, auth):
        """回传掩码不应把密码覆盖成星号。"""
        from app.core.config import get_settings, save_settings

        save_settings({"QBITTORRENT_PASSWORD": "realpass"})
        get_settings(reload=True)

        client.post(
            "/api/v1/config",
            headers=auth,
            json={"config": {"qbittorrent_password": "********"}},
        )
        assert get_settings(reload=True).qbittorrent_password == "realpass"

    def test_save_config_applies_real_value(self, client, auth):
        from app.core.config import get_settings

        response = client.post(
            "/api/v1/config",
            headers=auth,
            json={"config": {"qbittorrent_url": "http://192.168.1.9:8080"}},
        )
        assert response.json()["code"] == 200
        assert get_settings(reload=True).qbittorrent_url == "http://192.168.1.9:8080"

    def test_unknown_key_ignored(self, client, auth):
        response = client.post(
            "/api/v1/config", headers=auth, json={"config": {"not_a_real_key": "x"}}
        )
        assert response.json()["code"] == 200

    def test_version(self, client):
        assert client.get("/api/v1/version").json()["data"]["version"]

    def test_cron_list(self, client, auth):
        jobs = client.get("/api/v1/cron", headers=auth).json()["data"]["jobs"]
        assert isinstance(jobs, list)
        # 启动时注册的固定间隔任务应在列表中
        assert any(job["id"] == "pt_wait" for job in jobs)

    def test_run_unknown_task(self, client, auth):
        response = client.post("/api/v1/task?job_id=nope", headers=auth)
        assert response.json()["code"] == 404


class TestSubscribe:
    def test_subscribe_and_list(self, client, auth):
        from app.database.models import CodeStatus

        assert client.post(
            "/api/v1/codes/sub", headers=auth, json={"code": "abp984"}
        ).json()["code"] == 200

        data = client.get(
            f"/api/v1/codes/list?status={CodeStatus.SUBSCRIBED}", headers=auth
        ).json()["data"]
        # 番号应被标准化成 ABP-984
        assert any(item["code"] == "ABP-984" for item in data["items"])

    def test_cancel(self, client, auth):
        client.post("/api/v1/codes/sub", headers=auth, json={"code": "SSIS-001"})
        assert client.post(
            "/api/v1/codes/cancel", headers=auth, json={"code": "SSIS-001"}
        ).json()["code"] == 200

    def test_cancel_missing_code(self, client, auth):
        response = client.post(
            "/api/v1/codes/cancel", headers=auth, json={"code": "ZZZZ-999"}
        )
        assert response.json()["code"] == 404

    def test_pagination(self, client, auth):
        data = client.get("/api/v1/codes/list?page=1&size=5", headers=auth).json()["data"]
        assert data["size"] == 5
        assert len(data["items"]) <= 5

    def test_search_empty_result(self, client, auth):
        data = client.get("/api/v1/search?keyword=NOSUCH-999", headers=auth).json()["data"]
        assert data["items"] == []

    def test_search_finds_subscribed(self, client, auth):
        client.post("/api/v1/codes/sub", headers=auth, json={"code": "MIDE-777"})
        data = client.get("/api/v1/search?keyword=MIDE", headers=auth).json()["data"]
        assert any(item["code"] == "MIDE-777" for item in data["items"])

    def test_torrents_without_sites_returns_empty(self, client, auth):
        """未配置 PT 站时不应报错。"""
        data = client.get("/api/v1/torrents?code=ABP-984", headers=auth).json()["data"]
        assert data["items"] == []

    def test_release_today(self, client, auth):
        assert "items" in client.get("/api/v1/codes/release_today", headers=auth).json()["data"]

    def test_recommend(self, client, auth):
        assert "items" in client.get("/api/v1/codes/recommend", headers=auth).json()["data"]


class TestActors:
    def test_subscribe_and_list(self, client, auth):
        assert client.post(
            "/api/v1/actors/sub", headers=auth,
            json={"name": "测试演员", "limit_date": "2024-01-01"},
        ).json()["code"] == 200

        data = client.get("/api/v1/actors", headers=auth).json()["data"]
        assert any(item["name"] == "测试演员" for item in data["items"])

    def test_cancel(self, client, auth):
        client.post("/api/v1/actors/sub", headers=auth, json={"name": "临时演员"})
        assert client.post(
            "/api/v1/actors/cancel", headers=auth, json={"name": "临时演员"}
        ).json()["code"] == 200

    def test_cancel_missing(self, client, auth):
        response = client.post(
            "/api/v1/actors/cancel", headers=auth, json={"name": "不存在"}
        )
        assert response.json()["code"] == 404


class TestPicProxy:
    def test_rejects_non_whitelisted_host(self, client):
        assert client.get("/api/v1/image-proxy?url=http://evil.com/x.jpg").status_code == 403

    def test_missing_url(self, client):
        assert client.get("/api/v1/image-proxy?url=").status_code == 400


class TestMessageCommands:
    """指令解析不依赖外部服务，可直接单测。"""

    def test_help(self):
        from app.api.endpoints.message import _dispatch_command
        assert "可用指令" in _dispatch_command("/help")

    def test_status(self):
        from app.api.endpoints.message import _dispatch_command
        assert "统计" in _dispatch_command("/status")

    def test_sub_command(self):
        from app.api.endpoints.message import _dispatch_command
        assert "ABP-985" in _dispatch_command("/sub abp985")

    def test_bare_code_subscribes(self):
        from app.api.endpoints.message import _dispatch_command
        assert "SSIS-002" in _dispatch_command("SSIS-002")

    def test_unrecognized_returns_empty(self):
        from app.api.endpoints.message import _dispatch_command
        assert _dispatch_command("随便聊两句") == ""

    def test_sub_without_argument(self):
        from app.api.endpoints.message import _dispatch_command
        assert "用法" in _dispatch_command("/sub")

    def test_bare_message_subscribes_every_code(self):
        from app.api.endpoints.message import _dispatch_command
        reply = _dispatch_command(
            "我现在找到了nhdta-800、nhdta-526、nhdta-704、nhdtb-424、nhdtb-301、nhdtb-158"
        )
        for code in ("NHDTA-800", "NHDTA-526", "NHDTA-704",
                     "NHDTB-424", "NHDTB-301", "NHDTB-158"):
            assert code in reply

    def test_lowercase_code_in_sentence(self):
        from app.api.endpoints.message import _dispatch_command
        assert "JUL-915" in _dispatch_command("求 jul915")

    def test_url_alone_is_ignored(self):
        from app.api.endpoints.message import _dispatch_command
        assert _dispatch_command("https://javdb.com/v/abc123") == ""

    def test_block_prefix_filters_subscription(self, monkeypatch):
        from app.api.endpoints import message
        settings = message.get_settings()
        monkeypatch.setattr(settings, "msg_block_prefixes", "NHDTB", raising=False)
        reply = message._dispatch_command("nhdta-800、nhdtb-424")
        assert "NHDTA-800" in reply
        assert "已过滤" in reply

    def test_sub_accepts_multiple_codes(self):
        from app.api.endpoints.message import _dispatch_command
        reply = _dispatch_command("/sub abp985 ssis-002")
        assert "ABP-985" in reply and "SSIS-002" in reply


class TestMessageAutoDownload:
    """订阅后是否立即检索，以及入库番号的跳过。"""

    def test_existing_in_library_is_not_subscribed(self, monkeypatch):
        from app.api.endpoints import message

        settings = message.get_settings()
        monkeypatch.setattr(settings, "enable_auto_complete", True, raising=False)
        monkeypatch.setattr(
            "app.services.is_exist_server", lambda code: code == "ABP-100"
        )
        subscribed = []
        monkeypatch.setattr(
            "app.services.subscribe_code", lambda code: subscribed.append(code) or True
        )

        reply = message._dispatch_command("abp-100 ssis-200")
        assert subscribed == ["SSIS-200"]
        assert "媒体库已有" in reply and "ABP-100" in reply

    def test_library_check_skipped_when_disabled(self, monkeypatch):
        from app.api.endpoints import message

        settings = message.get_settings()
        monkeypatch.setattr(settings, "enable_auto_complete", False, raising=False)

        def _boom(code):
            raise AssertionError("关闭时不应查询媒体库")

        monkeypatch.setattr("app.services.is_exist_server", _boom)
        monkeypatch.setattr("app.services.subscribe_code", lambda code: True)
        assert "ABP-101" in message._dispatch_command("abp-101")

    def test_library_error_falls_back_to_subscribing(self, monkeypatch):
        """媒体库不可达时宁可多订，也别漏订。"""
        from app.api.endpoints import message

        settings = message.get_settings()
        monkeypatch.setattr(settings, "enable_auto_complete", True, raising=False)
        monkeypatch.setattr(
            "app.services.is_exist_server",
            lambda code: (_ for _ in ()).throw(RuntimeError("emby down")),
        )
        monkeypatch.setattr("app.services.subscribe_code", lambda code: True)
        assert "ABP-102" in message._dispatch_command("abp-102")

    def test_auto_download_off_does_not_search(self, monkeypatch):
        from app.api.endpoints import message

        settings = message.get_settings()
        monkeypatch.setattr(settings, "msg_auto_download", False, raising=False)
        monkeypatch.setattr(settings, "enable_auto_complete", False, raising=False)
        monkeypatch.setattr("app.services.subscribe_code", lambda code: True)

        called = []
        monkeypatch.setattr(
            "app.services.download_codes_async",
            lambda codes, **kw: called.append(codes),
        )
        reply = message._dispatch_command("abp-103")
        assert called == []
        assert "后台检索" not in reply

    def test_auto_download_on_triggers_search(self, monkeypatch):
        from app.api.endpoints import message

        settings = message.get_settings()
        monkeypatch.setattr(settings, "msg_auto_download", True, raising=False)
        monkeypatch.setattr(settings, "enable_auto_complete", False, raising=False)
        monkeypatch.setattr("app.services.subscribe_code", lambda code: True)

        called = []
        monkeypatch.setattr(
            "app.services.download_codes_async",
            lambda codes, **kw: called.append(list(codes)),
        )
        reply = message._dispatch_command("abp-104、ssis-205")
        assert called == [["ABP-104", "SSIS-205"]]
        assert "后台检索" in reply


class TestDownloadCodesAsync:
    def test_runs_in_background_and_reports_misses(self, monkeypatch):
        from app import services

        attempted = []

        def _download(code, torrent=None):
            attempted.append(code)
            return code == "ABP-200"   # 只有第一个搜到资源

        messages = []
        monkeypatch.setattr(services, "download_torrent", _download)
        monkeypatch.setattr(services, "send_message", lambda text: messages.append(text))

        threads = []
        monkeypatch.setattr(
            "app.utils.run_in_background",
            lambda func, *a, **kw: threads.append(func) or func(),
        )

        services.download_codes_async(["ABP-200", "SSIS-201"])
        assert attempted == ["ABP-200", "SSIS-201"]
        # 成功的由 download_torrent 自己通知，这里只汇总未命中的
        assert len(messages) == 1
        assert "SSIS-201" in messages[0] and "ABP-200" not in messages[0]

    def test_exception_counts_as_miss(self, monkeypatch):
        from app import services

        monkeypatch.setattr(
            services,
            "download_torrent",
            lambda code, torrent=None: (_ for _ in ()).throw(RuntimeError("pt down")),
        )
        messages = []
        monkeypatch.setattr(services, "send_message", lambda text: messages.append(text))
        monkeypatch.setattr("app.utils.run_in_background", lambda func, *a, **kw: func())

        services.download_codes_async(["ABP-300"])
        assert "ABP-300" in messages[0]

    def test_empty_list_is_noop(self, monkeypatch):
        from app import services

        monkeypatch.setattr(
            "app.utils.run_in_background",
            lambda func, *a, **kw: (_ for _ in ()).throw(AssertionError("不该起线程")),
        )
        services.download_codes_async([])
