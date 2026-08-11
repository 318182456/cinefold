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


class TestWebhookUrl:
    """外网地址补全成完整回调地址。"""

    def test_bare_domain_gets_https_and_path(self):
        from app.api.endpoints.config import _webhook_url
        assert _webhook_url("example.com") == "https://example.com/api/v1/message"

    def test_trailing_slash_is_trimmed(self):
        from app.api.endpoints.config import _webhook_url
        assert _webhook_url("https://a.com/") == "https://a.com/api/v1/message"

    def test_full_path_is_kept_as_is(self):
        from app.api.endpoints.config import _webhook_url
        url = "https://a.com/api/v1/message"
        assert _webhook_url(url) == url

    def test_empty_returns_empty(self):
        from app.api.endpoints.config import _webhook_url
        assert _webhook_url("") == ""


class TestTelegramUpdateHandling:
    """webhook 与 polling 共用的消息入口。"""

    def test_whitelist_blocks_stranger(self, monkeypatch):
        from app.api.endpoints import message

        settings = message.get_settings()
        monkeypatch.setattr(settings, "telegram_whitelist", "111", raising=False)
        replied = []
        monkeypatch.setattr(
            "app.services.reply_text_msg",
            lambda *args: replied.append(args) or True,
        )

        message.handle_telegram_update({
            "message": {"text": "ssis-002", "chat": {"id": 999}, "message_id": 1},
        })
        assert replied == []

    def test_whitelisted_user_gets_reply(self, monkeypatch):
        from app.api.endpoints import message

        settings = message.get_settings()
        monkeypatch.setattr(settings, "telegram_whitelist", "111", raising=False)
        monkeypatch.setattr("app.services.subscribe_code", lambda code: True)
        replied = []
        monkeypatch.setattr(
            "app.services.reply_text_msg",
            lambda *args: replied.append(args) or True,
        )

        message.handle_telegram_update({
            "message": {"text": "ssis-002", "chat": {"id": 111}, "message_id": 7},
        })
        assert len(replied) == 1
        assert "SSIS-002" in replied[0][0]

    def test_edited_message_is_handled(self, monkeypatch):
        from app.api.endpoints import message

        settings = message.get_settings()
        monkeypatch.setattr(settings, "telegram_whitelist", "", raising=False)
        monkeypatch.setattr("app.services.subscribe_code", lambda code: True)
        replied = []
        monkeypatch.setattr(
            "app.services.reply_text_msg",
            lambda *args: replied.append(args) or True,
        )

        message.handle_telegram_update({
            "edited_message": {"text": "abp-985", "chat": {"id": 1}, "message_id": 2},
        })
        assert len(replied) == 1

    def test_empty_text_is_ignored(self, monkeypatch):
        from app.api.endpoints import message

        replied = []
        monkeypatch.setattr(
            "app.services.reply_text_msg",
            lambda *args: replied.append(args) or True,
        )
        message.handle_telegram_update({"message": {"chat": {"id": 1}}})
        assert replied == []


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


class TestDownloadedStatusFix:
    """有下载记录却卡在 SUBSCRIBED 的番号要被补正，否则每轮都重搜。"""

    def _prepare(self, code, status):
        from app.database.base import DBBase
        from app.database.models import Code, CodeStatus, History
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            session.merge(Code(code=code, status=status))
            session.merge(History(hash=f"hash-{code}", code=code))
        return CodeStatus

    def test_status_is_corrected_to_downloading(self):
        from app import services
        from app.database.models import Code, CodeStatus
        from app.database.session import session_scope

        code = "FIXME-001"
        self._prepare(code, CodeStatus.SUBSCRIBED)

        assert services.download_torrent(code) is False
        with session_scope() as session:
            assert session.get(Code, code).status == CodeStatus.DOWNLOADING

    def test_other_status_is_left_alone(self):
        """已完成的不该被拉回下载中。"""
        from app import services
        from app.database.models import Code, CodeStatus
        from app.database.session import session_scope

        code = "FIXME-002"
        self._prepare(code, CodeStatus.DOWNLOADED)

        assert services.download_torrent(code) is False
        with session_scope() as session:
            assert session.get(Code, code).status == CodeStatus.DOWNLOADED


class TestLocalCodeSearch:
    """本地库命中，避免每次都去远程抓情报。"""

    def _seed(self, code, title="标题"):
        from app.database.base import DBBase
        from app.database.models import Code
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            session.merge(Code(code=code, title=title))

    def test_input_without_hyphen_hits_local(self):
        """用户输入 jul915，库里存的是 JUL-915。"""
        from app import services

        self._seed("JUL-915")
        found = services.search_code("jul915")
        assert [i["code"] for i in found] == ["JUL-915"]

    def test_exact_code_still_works(self):
        from app import services

        self._seed("ABP-777")
        assert [i["code"] for i in services.search_code("ABP-777")] == ["ABP-777"]

    def test_title_search_still_works(self):
        from app import services

        self._seed("SSIS-888", title="独特的标题关键词")
        found = services.search_code("独特的标题")
        assert [i["code"] for i in found] == ["SSIS-888"]

    def test_blank_keyword_returns_empty(self):
        from app import services

        assert services.search_code("   ") == []


class TestCacheRemoteCodes:
    """远程抓回的情报要落库，且不能动已有状态。"""

    def test_new_code_is_saved(self):
        from app import services
        from app.database.base import DBBase
        from app.database.models import Code
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        services.cache_remote_codes([{"code": "NEW-001", "title": "新标题"}])

        with session_scope() as session:
            row = session.get(Code, "NEW-001")
            assert row is not None and row.title == "新标题"

    def test_existing_status_is_preserved(self):
        """已订阅的番号不能被远程数据重置状态。"""
        from app import services
        from app.database.base import DBBase
        from app.database.models import Code, CodeStatus
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            session.merge(Code(code="KEEP-001", status=CodeStatus.SUBSCRIBED))

        services.cache_remote_codes([
            {"code": "KEEP-001", "title": "补上标题", "status": CodeStatus.NONE},
        ])

        with session_scope() as session:
            row = session.get(Code, "KEEP-001")
            assert row.status == CodeStatus.SUBSCRIBED
            assert row.title == "补上标题"

    def test_existing_value_is_not_overwritten(self):
        from app import services
        from app.database.base import DBBase
        from app.database.models import Code
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            session.merge(Code(code="KEEP-002", title="原标题"))

        services.cache_remote_codes([{"code": "KEEP-002", "title": "远程标题"}])

        with session_scope() as session:
            assert session.get(Code, "KEEP-002").title == "原标题"

    def test_empty_input_is_noop(self):
        from app import services
        assert services.cache_remote_codes([]) == 0


class TestVrCodeFilter:
    """VR 要在番号层就拦住，而不是等种子层——种子名未必带 VR 标记。"""

    def _seed(self, code, title=""):
        from app.database.base import DBBase
        from app.database.models import Code
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            session.merge(Code(code=code, title=title))

    def _set_exclude_vr(self, monkeypatch, value):
        from app import services

        settings = services.get_settings()
        monkeypatch.setattr(
            settings, "default_filter", {"exclude_vr": value}, raising=False
        )

    def test_vr_title_blocks_subscribe(self, monkeypatch):
        from app import services
        from app.database.models import Code, CodeStatus
        from app.database.session import session_scope

        self._seed("SSR-028", "【VR】8KVR 女体観察")
        self._set_exclude_vr(monkeypatch, True)

        assert services.subscribe_code("SSR-028") is False
        with session_scope() as session:
            assert session.get(Code, "SSR-028").status == CodeStatus.NONE

    def test_normal_code_still_subscribes(self, monkeypatch):
        from app import services
        from app.database.models import Code, CodeStatus
        from app.database.session import session_scope

        self._seed("SSNI-380", "絶対領域")
        self._set_exclude_vr(monkeypatch, True)
        monkeypatch.setattr(services, "send_subscribe_message", lambda code: 0)

        assert services.subscribe_code("SSNI-380") is True
        with session_scope() as session:
            assert session.get(Code, "SSNI-380").status == CodeStatus.SUBSCRIBED

    def test_vr_prefix_blocks_even_without_title(self, monkeypatch):
        from app import services

        self._seed("DSVR-1234")
        self._set_exclude_vr(monkeypatch, True)
        assert services.subscribe_code("DSVR-1234") is False

    def test_disabled_switch_lets_vr_through(self, monkeypatch):
        from app import services

        self._seed("SSR-029", "【VR】作品")
        self._set_exclude_vr(monkeypatch, False)
        monkeypatch.setattr(services, "send_subscribe_message", lambda code: 0)

        assert services.subscribe_code("SSR-029") is True

    def test_download_also_blocks_vr(self, monkeypatch):
        """种子名不带 VR 时，下载环节靠番号情报兜底。"""
        from app import services

        self._seed("SSR-030", "【VR】作品")
        self._set_exclude_vr(monkeypatch, True)
        assert services.download_torrent("SSR-030") is False


class TestActorSubscribeScope:
    """演员订阅不能把全部历史作品一次性拉进来。"""

    def _seed(self, entries):
        from app.database.base import DBBase
        from app.database.models import Code
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            for code, release in entries:
                session.merge(Code(code=code, casts="测试演员", release_date=release))

    def test_no_limit_date_only_takes_recent(self, monkeypatch):
        from datetime import datetime, timedelta

        from app import services
        from app.database.models import Code, CodeStatus
        from app.database.session import session_scope

        recent = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        self._seed([("OLD-001", "2015-01-01"), ("NEW-001", recent)])
        monkeypatch.setattr(
            services.get_settings(), "default_filter", {}, raising=False
        )

        services._subscribe_actor_new_works("测试演员", None)

        with session_scope() as session:
            assert session.get(Code, "OLD-001").status == CodeStatus.NONE
            assert session.get(Code, "NEW-001").status == CodeStatus.SUBSCRIBED

    def test_explicit_limit_date_is_respected(self, monkeypatch):
        from app import services
        from app.database.models import Code, CodeStatus
        from app.database.session import session_scope

        self._seed([("A-100", "2020-01-01"), ("A-200", "2024-06-01")])
        monkeypatch.setattr(
            services.get_settings(), "default_filter", {}, raising=False
        )

        services._subscribe_actor_new_works("测试演员", "2023-01-01")

        with session_scope() as session:
            assert session.get(Code, "A-100").status == CodeStatus.NONE
            assert session.get(Code, "A-200").status == CodeStatus.SUBSCRIBED

    def test_batch_is_capped(self, monkeypatch):
        """单轮新增有上限，不会一次刷爆。"""
        from app import services
        monkeypatch.setattr(services, "ACTOR_SUBSCRIBE_LIMIT", 5)
        monkeypatch.setattr(
            services.get_settings(), "default_filter", {}, raising=False
        )

        self._seed([(f"CAP-{i:03d}", "2024-06-01") for i in range(20)])
        added = services._subscribe_actor_new_works("测试演员", "2023-01-01")
        assert added == 5

    def test_subscribe_actor_defaults_to_today(self):
        """新订阅演员时默认从今天算起，不回溯历史。"""
        from datetime import datetime

        from app import services
        from app.database.base import DBBase
        from app.database.models import Actor
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            existing = session.get(Actor, "新演员")
            if existing is not None:
                session.delete(existing)

        services.subscribe_actor("新演员")
        with session_scope() as session:
            row = session.get(Actor, "新演员")
            assert row.limit_date == datetime.now().strftime("%Y-%m-%d")


class TestRousiTokenCache:
    """token 缓存在类上，避免每次搜索都重新登录。"""

    def test_login_once_across_instances(self, monkeypatch):
        from app.modules.ptsite.rousi import Rousi

        Rousi.reset_token_cache()
        logins = []

        def _login(self):
            logins.append(1)
            # 不带 exp 的 token，_is_expiring 解不出时视为有效
            return "fake.token.value"

        monkeypatch.setattr(Rousi, "_login", _login)
        monkeypatch.setattr(
            Rousi, "__init__",
            lambda self, **kw: self.__dict__.update(
                host="h", passkey="", username="u", password="p",
                proxy=None, _token="",
            ),
        )

        assert Rousi().token == "fake.token.value"
        assert Rousi().token == "fake.token.value"
        assert Rousi().token == "fake.token.value"
        assert len(logins) == 1

        Rousi.reset_token_cache()

    def test_reset_forces_relogin(self, monkeypatch):
        from app.modules.ptsite.rousi import Rousi

        Rousi.reset_token_cache()
        logins = []
        monkeypatch.setattr(
            Rousi, "_login",
            lambda self: logins.append(1) or "fake.token.value",
        )
        monkeypatch.setattr(
            Rousi, "__init__",
            lambda self, **kw: self.__dict__.update(
                host="h", passkey="", username="u", password="p",
                proxy=None, _token="",
            ),
        )

        Rousi().token
        Rousi.reset_token_cache()
        Rousi().token
        assert len(logins) == 2

        Rousi.reset_token_cache()
