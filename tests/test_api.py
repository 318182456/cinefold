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
        """写进去的值要真的落到配置里。

        测完必须清掉：这个地址会写进测试用的 .env，后面的用例（如 medialink 的
        联动删除）碰到已配置的下载器就会真的去连 192.168.1.9，连不通时 TCP
        要挂到超时，整套测试会卡死在那里。
        """
        from app.core.config import get_settings, save_settings

        try:
            response = client.post(
                "/api/v1/config",
                headers=auth,
                json={"config": {"qbittorrent_url": "http://192.168.1.9:8080"}},
            )
            assert response.json()["code"] == 200
            assert get_settings(reload=True).qbittorrent_url == "http://192.168.1.9:8080"
        finally:
            save_settings({"QBITTORRENT_URL": ""})
            get_settings(reload=True)

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

    def test_recommend_pagination(self, client, auth):
        """推荐分页：两页不重叠，且 total 是筛选后的总数。"""
        from app.database.models import Code, CodeStatus
        from app.database.session import session_scope

        with session_scope() as session:
            for i in range(5):
                session.merge(Code(
                    code=f"REC-{i:03d}", status=CodeStatus.NONE,
                    star=9.0 - i * 0.1, title="t",
                ))

        first = client.get("/api/v1/codes/recommend?limit=2&page=1", headers=auth).json()["data"]
        second = client.get("/api/v1/codes/recommend?limit=2&page=2", headers=auth).json()["data"]

        assert first["total"] >= 5
        assert len(first["items"]) == 2
        assert not {i["code"] for i in first["items"]} & {i["code"] for i in second["items"]}


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


class TestResubscribeResetsState:
    """订阅即从头来过：旧下载记录必须清掉。

    用户在下载器里删掉种子后 History 仍在，番号会被 download_torrent 判成
    「已下载过」而永不搜种，状态又被推到 DOWNLOADING 无人推进，于是卡死在
    「下载中」。订阅入口清干净才解得开。
    """

    def _seed(self, code, status, hashes=("h1",)):
        from app.database.base import DBBase
        from app.database.models import Code, CodeStatus, History
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            session.merge(Code(code=code, status=status))
            for h in hashes:
                session.merge(History(hash=f"{h}-{code}", code=code))
        return CodeStatus

    @staticmethod
    def _downloader(monkeypatch, alive_hashes=()):
        """模拟下载器，只认 alive_hashes 里的种子还在。"""
        from app import services

        class _Fake:
            def monitor_torrent(self, hashes=None):
                return [{"hash": h} for h in (hashes or []) if h in alive_hashes]

        monkeypatch.setattr(
            services.downloadclient, "get_download_client", lambda name="": _Fake(),
        )

    def test_vanished_history_is_cleared(self, monkeypatch):
        """种子已从下载器消失，记录该清掉，让它重新搜种。"""
        from app import services
        from app.database.models import Code, CodeStatus, History
        from app.database.session import session_scope

        code = "REDO-001"
        self._seed(code, CodeStatus.DOWNLOADING, hashes=("h1", "h2"))
        self._downloader(monkeypatch)  # 一个都不在

        assert services.subscribe_code(code) is True
        with session_scope() as session:
            assert session.get(Code, code).status == CodeStatus.SUBSCRIBED
            assert session.query(History).filter(History.code == code).count() == 0

    def test_alive_history_is_kept(self, monkeypatch):
        """种子还在下载器里就保留记录 —— 那仍是有效的番号↔hash 关联，
        清掉只会白搜一轮，推送时下载器回「已存在」，关联反倒丢了。"""
        from app import services
        from app.database.models import Code, CodeStatus, History
        from app.database.session import session_scope

        code = "KEEP-001"
        self._seed(code, CodeStatus.DOWNLOADING, hashes=("h1",))
        self._downloader(monkeypatch, alive_hashes={f"h1-{code}"})

        assert services.subscribe_code(code) is True
        with session_scope() as session:
            assert session.get(Code, code).status == CodeStatus.SUBSCRIBED
            assert session.query(History).filter(History.code == code).count() == 1

    def test_partial_vanish_only_drops_gone_ones(self, monkeypatch):
        """转种场景下一个番号有多条记录，只清没了的那些。"""
        from app import services
        from app.database.models import CodeStatus, History
        from app.database.session import session_scope

        code = "MIXED-001"
        self._seed(code, CodeStatus.DOWNLOADING, hashes=("h1", "h2"))
        self._downloader(monkeypatch, alive_hashes={f"h1-{code}"})

        services.subscribe_code(code)
        with session_scope() as session:
            left = [r.hash for r in session.query(History).filter(History.code == code)]
            assert left == [f"h1-{code}"]

    def test_no_downloader_keeps_history(self, monkeypatch):
        """下载器没配置时无法判断种子死活，宁可留着记录。"""
        from app import services
        from app.database.models import CodeStatus, History
        from app.database.session import session_scope

        code = "NOCLIENT-001"
        self._seed(code, CodeStatus.DOWNLOADING)
        monkeypatch.setattr(
            services.downloadclient, "get_download_client", lambda name="": None,
        )

        services.subscribe_code(code)
        with session_scope() as session:
            assert session.query(History).filter(History.code == code).count() == 1

    def test_cleared_code_searches_again(self, monkeypatch):
        """清掉记录后，download_torrent 不该再判「已下载过」。"""
        from app import services
        from app.database.models import CodeStatus

        code = "REDO-002"
        self._seed(code, CodeStatus.DOWNLOADING)
        self._downloader(monkeypatch)
        services.subscribe_code(code)

        # 走到搜种这一步就说明没被 _is_downloaded 短路
        searched = []
        monkeypatch.setattr(
            services, "find_torrent",
            lambda c: searched.append(c) or None,
        )
        services.download_torrent(code)
        assert searched == [code], "重新订阅后应该重新搜种"

    def test_other_codes_untouched(self, monkeypatch):
        """只清自己的记录，别误伤别的番号。"""
        from app import services
        from app.database.models import CodeStatus, History
        from app.database.session import session_scope

        mine, other = "REDO-003", "KEEP-003"
        self._seed(mine, CodeStatus.DOWNLOADING)
        self._seed(other, CodeStatus.DOWNLOADING)
        self._downloader(monkeypatch)

        services.subscribe_code(mine)
        with session_scope() as session:
            assert session.query(History).filter(History.code == mine).count() == 0
            assert session.query(History).filter(History.code == other).count() == 1

    def test_medialink_is_kept(self):
        """已入库的硬链接对应真实文件，重新订阅不该删。"""
        from app import services
        from app.database.models import CodeStatus, MediaLink
        from app.database.session import session_scope

        code = "REDO-004"
        self._seed(code, CodeStatus.COMPLETED)
        with session_scope() as session:
            session.merge(MediaLink(
                link_path=f"/media/{code}.mp4", code=code, source_path=f"/dl/{code}.mp4",
            ))

        services.subscribe_code(code)
        with session_scope() as session:
            assert session.query(MediaLink).filter(MediaLink.code == code).count() == 1


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


class TestRemoteSearchMismatch:
    """资源站搜不到时未必老实报空，返回的番号对不上就该丢掉。

    实际遇到：搜 SONS-183 拿回 NASK-183，页面上显示成搜到了，
    情报还落进了本地库。
    """

    def _client(self, client, auth):
        return client, auth

    def test_mismatched_code_is_dropped(self, client, auth, monkeypatch):
        from app.modules import ladysite

        monkeypatch.setattr(
            ladysite, "search_code",
            lambda kw: [{"code": "NASK-183", "title": "别的片子"}],
        )

        data = client.get("/api/v1/search?keyword=sons183", headers=auth).json()["data"]
        assert data["items"] == [], "番号不符的结果不该返回"

    def test_matching_code_is_kept(self, client, auth, monkeypatch):
        from app.modules import ladysite

        monkeypatch.setattr(
            ladysite, "search_code",
            lambda kw: [{"code": "SONS-183", "title": "正确的片子"}],
        )

        data = client.get("/api/v1/search?keyword=sons183", headers=auth).json()["data"]
        assert [i["code"] for i in data["items"]] == ["SONS-183"]

    def test_keyword_search_is_not_checked(self, client, auth, monkeypatch):
        """按标题/演员搜索时，返回值本就不该等于关键词。"""
        from app.modules import ladysite

        monkeypatch.setattr(
            ladysite, "search_code",
            lambda kw: [{"code": "ABC-123", "title": "某演员的作品"}],
        )

        data = client.get("/api/v1/search?keyword=某演员", headers=auth).json()["data"]
        assert [i["code"] for i in data["items"]] == ["ABC-123"]


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


class TestBtAutoDownload:
    """BT 源可以只参与搜索，不进自动选种。"""

    def _torrents(self):
        from app.schemas.torrent import Torrent
        return [
            Torrent(id=1, site="BT", title="t1", seeders=100),
            Torrent(id=2, site="MTeam", title="t2", seeders=10),
        ]

    def test_bt_skipped_when_disabled(self, monkeypatch):
        from app import services

        monkeypatch.setattr(services, "search_torrents", lambda code: self._torrents())
        monkeypatch.setattr(
            services.get_settings(), "bt_auto_download", False, raising=False
        )
        assert services.find_torrent("ABC-123").site == "MTeam"

    def test_bt_used_when_enabled(self, monkeypatch):
        from app import services

        monkeypatch.setattr(services, "search_torrents", lambda code: self._torrents())
        monkeypatch.setattr(
            services.get_settings(), "bt_auto_download", True, raising=False
        )
        assert services.find_torrent("ABC-123").site == "BT"

    def test_none_when_only_bt_and_disabled(self, monkeypatch):
        from app import services
        from app.schemas.torrent import Torrent

        monkeypatch.setattr(
            services, "search_torrents",
            lambda code: [Torrent(id=1, site="BT", title="t")],
        )
        monkeypatch.setattr(
            services.get_settings(), "bt_auto_download", False, raising=False
        )
        assert services.find_torrent("ABC-123") is None

    def test_interface_reported_site_does_not_bypass_filter(self, monkeypatch):
        """接口自报 site 时，BT 源仍必须被标成 BT。

        从前是 `site or self.name`，接口只要回一个 site 字段，种子就顶着
        别人的站名混过 find_torrent 的过滤，关掉开关照样自动下载。
        """
        from app import services
        from app.modules.bt.bt import BT

        class _Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"data": [
                    {"id": 1, "site": "MTeam", "title": "t1", "seeders": 100},
                ]}

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def request(self, *_, **__):
                return _Response()

        monkeypatch.setattr("app.modules.bt.bt.httpx.Client", lambda **kw: _Client())

        source = BT(url="http://example.invalid/api")
        torrents = source.search("ABC-123")

        assert torrents[0].site == "BT"
        # 原站名保留下来只作展示，不参与过滤
        assert torrents[0].source_site == "MTeam"
        assert torrents[0].display_site == "BT · MTeam"

        monkeypatch.setattr(services, "search_torrents", lambda code: torrents)
        monkeypatch.setattr(
            services.get_settings(), "bt_auto_download", False, raising=False
        )
        assert services.find_torrent("ABC-123") is None


class TestDetailRace:
    """番号情报多站并发，谁先出结果用谁。"""

    def test_returns_first_success(self, monkeypatch):
        import time

        from app.modules import ladysite

        def fake_fetch(site_name, code):
            if site_name == "javbus":
                time.sleep(0.5)          # 慢的那个
                return {"code": code, "title": "from bus"}
            return {"code": code, "title": "from db"}

        monkeypatch.setattr(ladysite, "_fetch_detail", fake_fetch)
        monkeypatch.setattr(ladysite, "_enabled_sites", lambda: ("javbus", "javdb"))

        start = time.perf_counter()
        detail = ladysite.get_code_detail("ABC-123")
        elapsed = time.perf_counter() - start

        # 快的先返回，不必等慢的那个
        assert detail["title"] == "from db"
        assert elapsed < 0.4

    def test_falls_back_when_first_empty(self, monkeypatch):
        from app.modules import ladysite

        def fake_fetch(site_name, code):
            return {"code": code, "title": "ok"} if site_name == "javdb" else {}

        monkeypatch.setattr(ladysite, "_fetch_detail", fake_fetch)
        monkeypatch.setattr(ladysite, "_enabled_sites", lambda: ("javbus", "javdb"))
        assert ladysite.get_code_detail("ABC-123")["title"] == "ok"

    def test_all_empty_returns_empty(self, monkeypatch):
        from app.modules import ladysite

        monkeypatch.setattr(ladysite, "_fetch_detail", lambda s, c: {})
        monkeypatch.setattr(ladysite, "_enabled_sites", lambda: ("javbus", "javdb"))
        assert ladysite.get_code_detail("ABC-123") == {}


class TestBulkCancel:
    """批量取消订阅——只动已订阅的，别碰下载中/已入库。"""

    def _seed(self, entries):
        from sqlalchemy import select

        from app.database.base import DBBase
        from app.database.models import Code
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            # 清掉上一个用例的数据，避免互相干扰
            for row in session.scalars(select(Code)).all():
                session.delete(row)
            for code, status, release, title in entries:
                session.merge(Code(
                    code=code, status=status, release_date=release, title=title,
                ))

    def test_dry_run_changes_nothing(self):
        from app import services
        from app.database.models import Code, CodeStatus
        from app.database.session import session_scope

        self._seed([("OLD-1", CodeStatus.SUBSCRIBED, "2015-01-01", "t")])
        result = services.bulk_cancel_subscribe(before_date="2020-01-01")

        assert result["matched"] == 1
        assert result["cancelled"] == 0
        with session_scope() as session:
            assert session.get(Code, "OLD-1").status == CodeStatus.SUBSCRIBED

    def test_executes_when_not_dry_run(self):
        from app import services
        from app.database.models import Code, CodeStatus
        from app.database.session import session_scope

        self._seed([("OLD-2", CodeStatus.SUBSCRIBED, "2015-01-01", "t")])
        result = services.bulk_cancel_subscribe(
            before_date="2020-01-01", dry_run=False
        )

        assert result["cancelled"] == 1
        with session_scope() as session:
            assert session.get(Code, "OLD-2").status == CodeStatus.NONE

    def test_other_statuses_are_untouched(self):
        """下载中、已入库的不该被清掉。"""
        from app import services
        from app.database.models import Code, CodeStatus
        from app.database.session import session_scope

        self._seed([
            ("DL-1", CodeStatus.DOWNLOADING, "2015-01-01", "t"),
            ("OK-1", CodeStatus.COMPLETED, "2015-01-01", "t"),
            ("SUB-1", CodeStatus.SUBSCRIBED, "2015-01-01", "t"),
        ])
        services.bulk_cancel_subscribe(before_date="2020-01-01", dry_run=False)

        with session_scope() as session:
            assert session.get(Code, "DL-1").status == CodeStatus.DOWNLOADING
            assert session.get(Code, "OK-1").status == CodeStatus.COMPLETED
            assert session.get(Code, "SUB-1").status == CodeStatus.NONE

    def test_recent_ones_are_kept(self):
        from datetime import datetime, timedelta

        from app import services
        from app.database.models import Code, CodeStatus
        from app.database.session import session_scope

        recent = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        self._seed([
            ("OLD-3", CodeStatus.SUBSCRIBED, "2015-01-01", "t"),
            ("NEW-3", CodeStatus.SUBSCRIBED, recent, "t"),
        ])
        services.bulk_cancel_subscribe(keep_recent_days=30, dry_run=False)

        with session_scope() as session:
            assert session.get(Code, "OLD-3").status == CodeStatus.NONE
            assert session.get(Code, "NEW-3").status == CodeStatus.SUBSCRIBED

    def test_only_vr_filters_by_title(self):
        from app import services
        from app.database.models import Code, CodeStatus
        from app.database.session import session_scope

        self._seed([
            ("VR-1", CodeStatus.SUBSCRIBED, "2015-01-01", "【VR】8KVR 作品"),
            ("NORMAL-1", CodeStatus.SUBSCRIBED, "2015-01-01", "普通作品"),
        ])
        result = services.bulk_cancel_subscribe(only_vr=True, dry_run=False)

        assert result["cancelled"] == 1
        with session_scope() as session:
            assert session.get(Code, "VR-1").status == CodeStatus.NONE
            assert session.get(Code, "NORMAL-1").status == CodeStatus.SUBSCRIBED

    def test_endpoint_rejects_empty_conditions(self, client, token):
        """不给条件时拒绝，避免一把清空。"""
        response = client.post(
            "/api/v1/codes/cancel/bulk",
            headers={"Authorization": f"Bearer {token}"},
            json={"dry_run": False},
        )
        assert response.json()["code"] == 400

    def test_batch_cancel_by_codes(self, client, token):
        """列表页多选取消：只动选中的那几个。"""
        from app.database.models import Code, CodeStatus
        from app.database.session import session_scope

        self._seed([
            ("PICK-1", CodeStatus.SUBSCRIBED, "2015-01-01", "t"),
            ("PICK-2", CodeStatus.SUBSCRIBED, "2015-01-01", "t"),
            ("KEEP-1", CodeStatus.SUBSCRIBED, "2015-01-01", "t"),
        ])
        body = client.post(
            "/api/v1/codes/cancel/batch",
            headers={"Authorization": f"Bearer {token}"},
            json={"codes": ["PICK-1", "PICK-2", "GHOST-9"]},
        ).json()

        assert body["code"] == 200
        assert body["data"]["cancelled"] == ["PICK-1", "PICK-2"]
        assert body["data"]["missing"] == ["GHOST-9"]
        with session_scope() as session:
            assert session.get(Code, "PICK-1").status == CodeStatus.NONE
            assert session.get(Code, "KEEP-1").status == CodeStatus.SUBSCRIBED

    def test_batch_endpoints_reject_empty_list(self, client, token):
        headers = {"Authorization": f"Bearer {token}"}
        for path in ("/api/v1/codes/cancel/batch", "/api/v1/codes/sub/batch"):
            assert client.post(path, headers=headers, json={"codes": []}).json()["code"] == 400

    def test_batch_subscribe_by_codes(self, client, token):
        """榜单等页面多选订阅，番号会被标准化。"""
        from app.database.models import Code, CodeStatus
        from app.database.session import session_scope

        body = client.post(
            "/api/v1/codes/sub/batch",
            headers={"Authorization": f"Bearer {token}"},
            json={"codes": ["stars789", "MIDE-778"]},
        ).json()

        assert body["code"] == 200
        assert body["data"]["subscribed"] == ["STARS-789", "MIDE-778"]
        with session_scope() as session:
            assert session.get(Code, "STARS-789").status == CodeStatus.SUBSCRIBED


class TestNexusBreaker:
    """站点不可用时熔断，别拿两万个订阅逐个去撞配额。"""

    def _site(self, html):
        import httpx

        from app.modules.ptsite.nexus import NexusSite

        class _Fake(NexusSite):
            name = "TESTPT"
            host = "https://example.test"

            def __init__(self):
                super().__init__(cookie="c")

        calls = []

        def fake_get(self, url, **kwargs):
            calls.append(url)
            return httpx.Response(200, text=html, request=httpx.Request("GET", url))

        return _Fake, fake_get, calls

    def test_trips_after_repeated_failures(self, monkeypatch):
        import httpx

        from app.modules.ptsite.nexus import FAILURE_THRESHOLD, NexusSite

        NexusSite.reset_breakers()
        Site, fake_get, calls = self._site("<html>短跳转页</html>")
        monkeypatch.setattr(httpx.Client, "get", fake_get)

        for _ in range(FAILURE_THRESHOLD + 5):
            Site().search("ABC-123")

        # 达到阈值后不再发请求
        assert len(calls) == FAILURE_THRESHOLD
        NexusSite.reset_breakers()

    def test_rate_limit_page_trips(self, monkeypatch):
        import httpx

        from app.modules.ptsite.nexus import FAILURE_THRESHOLD, NexusSite

        NexusSite.reset_breakers()
        Site, fake_get, calls = self._site(
            "您的账号今日访问次数已达上限，请明天再试!" + "x" * 9000
        )
        monkeypatch.setattr(httpx.Client, "get", fake_get)

        for _ in range(FAILURE_THRESHOLD + 3):
            Site().search("ABC-123")

        assert len(calls) == FAILURE_THRESHOLD
        NexusSite.reset_breakers()

    def test_reset_allows_retry(self, monkeypatch):
        import httpx

        from app.modules.ptsite.nexus import FAILURE_THRESHOLD, NexusSite

        NexusSite.reset_breakers()
        Site, fake_get, calls = self._site("<html>短</html>")
        monkeypatch.setattr(httpx.Client, "get", fake_get)

        for _ in range(FAILURE_THRESHOLD + 2):
            Site().search("ABC-123")
        before = len(calls)

        # 改了配置就该立刻重试
        NexusSite.reset_breakers()
        Site().search("ABC-123")
        assert len(calls) == before + 1
        NexusSite.reset_breakers()

    def test_success_resets_counter(self, monkeypatch):
        """偶发失败不该累积成熔断。"""
        import httpx

        from app.modules.ptsite.nexus import NexusSite

        NexusSite.reset_breakers()
        good = "<table class='torrents'>" + "x" * 9000 + "details.php</table>"
        pages = ["<html>短</html>", good] * 6
        calls = []

        def fake_get(self, url, **kwargs):
            html = pages[len(calls)]
            calls.append(url)
            return httpx.Response(200, text=html, request=httpx.Request("GET", url))

        Site, _, _ = self._site("")
        monkeypatch.setattr(httpx.Client, "get", fake_get)

        for _ in range(len(pages)):
            Site().search("ABC-123")

        # 失败与成功交替，计数被成功清零，始终不熔断
        assert len(calls) == len(pages)
        NexusSite.reset_breakers()


class TestTranslateStreak:
    """翻译的「连续失败」必须真的是连续，不能是累计。

    原先用 itertools.count 只增不减，成功的翻译不清零。于是零散失败攒够
    阈值也会误判服务不可用 —— 现场日志里「翻译连续失败，本轮提前结束」和
    「已翻译 4 个标题」同时出现，自相矛盾，且把本来能翻的剩余条目全丢了。
    """

    def _seed(self, n):
        from app.database.base import DBBase
        from app.database.models import Code
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            for i in range(n):
                session.merge(Code(code=f"TRN-{i:03d}", title=f"日文タイトル{i}",
                                   cn_title=""))

    def test_interleaved_failures_do_not_give_up(self, monkeypatch):
        """一次失败一次成功交替出现时，绝不能提前收工。"""
        from app import services

        self._seed(20)
        monkeypatch.setattr(services.translate, "is_available", lambda: True)

        calls = {"n": 0}

        def flaky(title):
            calls["n"] += 1
            # 奇数次失败、偶数次成功 —— 从未连续失败到阈值
            return "" if calls["n"] % 2 else f"译-{title}"

        monkeypatch.setattr(services, "translate_title", flaky)
        # 单线程跑，让交替顺序可预期
        monkeypatch.setattr(services, "TRANSLATE_WORKERS", 1)

        count = services.translate_codes(limit=20)
        # 一半成功，且没有因为累计失败而中断
        assert count == 10
        assert calls["n"] == 20

    def test_real_consecutive_failures_give_up(self, monkeypatch):
        """真的连续失败到阈值时要收工，别把剩下的全撞一遍超时。"""
        from app import services

        self._seed(30)
        monkeypatch.setattr(services.translate, "is_available", lambda: True)

        calls = {"n": 0}

        def always_fail(title):
            calls["n"] += 1
            return ""

        monkeypatch.setattr(services, "translate_title", always_fail)
        monkeypatch.setattr(services, "TRANSLATE_WORKERS", 1)

        assert services.translate_codes(limit=30) == 0
        # 撞到阈值就停，不该把 30 条全试一遍
        assert calls["n"] <= services.TRANSLATE_FAILURE_LIMIT + 1


class TestRedisCacheTTL:
    """Redis 缓存必须带过期时间。

    get_rank_cache 的 ttl 只管数据库那一侧（靠 create_time 判断），Redis
    这边不传 ex 就是永不过期。两边不一致时，配了 Redis 的部署里
    「媒体库有没有这个番号」会被永久缓存成「有」—— 番号删掉重下会被一直
    拦在门外（_split_existing 直接判为 existing，压根不调 subscribe_code），
    而 MEDIA_EXISTS_CACHE_TTL=600 形同虚设。
    """

    def test_media_exists_cache_sets_ttl(self, monkeypatch):
        from app import services

        captured = {}

        def fake_set(key, value, ttl=None):
            captured["key"] = key
            captured["value"] = value
            captured["ttl"] = ttl
            return True

        from app.core import redis as redis_cache
        monkeypatch.setattr(redis_cache, "set", fake_set)
        monkeypatch.setattr(services.mediaserver, "exists_in_any",
                            lambda code: True)
        monkeypatch.setattr(services, "get_rank_cache", lambda *a, **k: None)

        assert services.is_exist_server("MOON-042") is True
        assert captured["value"] == "1"
        # 关键：必须把 TTL 传下去，不能是 None
        assert captured["ttl"] == services.MEDIA_EXISTS_CACHE_TTL

    def test_set_rank_cache_passes_ttl_to_redis(self, monkeypatch):
        from app import services

        from app.core import redis as redis_cache
        seen = {}
        monkeypatch.setattr(
            redis_cache, "set",
            lambda key, value, ttl=None: seen.update(ttl=ttl) or True,
        )
        services.set_rank_cache("media", "X-1", "1", ttl=600)
        assert seen["ttl"] == 600

    def test_zero_ttl_means_no_expiry(self, monkeypatch):
        """榜单快照那类不传 ttl 的调用仍是永久缓存，行为不变。"""
        from app import services

        from app.core import redis as redis_cache
        seen = {}
        monkeypatch.setattr(
            redis_cache, "set",
            lambda key, value, ttl=None: seen.update(ttl=ttl) or True,
        )
        services.set_rank_cache("rank", "daily", "[]")
        assert seen["ttl"] is None


class TestResetCode:
    """重置番号：清掉拦住「删干净了却下不下来」的三处残留。"""

    def _seed(self, code="RST-001", status=2, hashes=("h1" * 20,)):
        from app.database.base import DBBase
        from app.database.models import Code, History
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            session.merge(Code(code=code, status=status))
            for h in hashes:
                session.merge(History(hash=h, code=code))

    def test_dry_run_changes_nothing(self, monkeypatch):
        from app import services
        from app.database.models import History
        from app.database.session import session_scope

        self._seed()
        monkeypatch.setattr(services.downloadclient, "get_download_client",
                            lambda name="": type("C", (), {
                                "monitor_torrent": lambda s, h=None: []})())

        out = services.reset_code_for_redownload("RST-001", dry_run=True)
        assert out["history_removed"] == ["h1" * 20]
        # 预览不能落库
        with session_scope() as session:
            assert session.get(History, "h1" * 20) is not None

    def test_clears_gone_history_and_status(self, monkeypatch):
        from app import services
        from app.database.models import Code, CodeStatus, History
        from app.database.session import session_scope

        self._seed()
        monkeypatch.setattr(services.downloadclient, "get_download_client",
                            lambda name="": type("C", (), {
                                "monitor_torrent": lambda s, h=None: []})())
        monkeypatch.setattr(services, "drop_media_exists_cache", lambda c="": True)

        out = services.reset_code_for_redownload("RST-001", dry_run=False)
        assert out["history_removed"] == ["h1" * 20]
        with session_scope() as session:
            assert session.get(History, "h1" * 20) is None
            assert session.get(Code, "RST-001").status == CodeStatus.NONE

    def test_keeps_history_of_live_torrents(self, monkeypatch):
        """种子还在做种的记录不能删 —— 那是有效关联。"""
        from app import services
        from app.database.models import History
        from app.database.session import session_scope

        self._seed(hashes=("a" * 40, "b" * 40))
        monkeypatch.setattr(
            services.downloadclient, "get_download_client",
            lambda name="": type("C", (), {
                "monitor_torrent": lambda s, h=None: [{"hash": "a" * 40}]})(),
        )
        monkeypatch.setattr(services, "drop_media_exists_cache", lambda c="": True)

        out = services.reset_code_for_redownload("RST-001", dry_run=False)
        assert out["history_kept"] == ["a" * 40]
        assert out["history_removed"] == ["b" * 40]
        with session_scope() as session:
            assert session.get(History, "a" * 40) is not None
            assert session.get(History, "b" * 40) is None

    def test_downloader_down_keeps_everything(self, monkeypatch):
        """下载器连不上时一条都不删 —— 无法判断种子死活，宁可白跑。"""
        from app import services
        from app.database.models import History
        from app.database.session import session_scope

        self._seed()
        monkeypatch.setattr(services.downloadclient, "get_download_client",
                            lambda name="": None)
        monkeypatch.setattr(services, "drop_media_exists_cache", lambda c="": True)

        out = services.reset_code_for_redownload("RST-001", dry_run=False)
        assert out["downloader_unavailable"] is True
        assert out["history_removed"] == []
        with session_scope() as session:
            assert session.get(History, "h1" * 20) is not None
