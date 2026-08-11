"""OIDC 与 Passkey 登录。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture
def settings():
    from app.core.config import get_settings
    return get_settings()


class TestOidcConfig:
    def test_not_configured_by_default(self, settings, monkeypatch):
        from app.modules.auth import oidc

        monkeypatch.setattr(settings, "oidc_enabled", False, raising=False)
        assert oidc.is_configured() is False
        assert oidc.public_info()["enabled"] is False

    def test_needs_all_three_fields(self, settings, monkeypatch):
        """开关打开但缺凭证时不算配置好。"""
        from app.modules.auth import oidc

        monkeypatch.setattr(settings, "oidc_enabled", True, raising=False)
        monkeypatch.setattr(settings, "oidc_issuer", "https://auth.test", raising=False)
        monkeypatch.setattr(settings, "oidc_client_id", "", raising=False)
        monkeypatch.setattr(settings, "oidc_client_secret", "s", raising=False)
        assert oidc.is_configured() is False

    def test_configured_when_complete(self, settings, monkeypatch):
        from app.modules.auth import oidc

        monkeypatch.setattr(settings, "oidc_enabled", True, raising=False)
        monkeypatch.setattr(settings, "oidc_issuer", "https://auth.test", raising=False)
        monkeypatch.setattr(settings, "oidc_client_id", "cid", raising=False)
        monkeypatch.setattr(settings, "oidc_client_secret", "sec", raising=False)
        monkeypatch.setattr(settings, "oidc_display_name", "公司账号", raising=False)

        assert oidc.is_configured() is True
        assert oidc.public_info()["display_name"] == "公司账号"


class TestOidcState:
    def test_state_is_single_use(self):
        from app.modules.auth import oidc

        with oidc._state_lock:
            oidc._states["abc"] = {
                "created": __import__("time").time(),
                "nonce": "n", "redirect_uri": "u", "next": "/",
            }

        assert oidc.pop_state("abc")["next"] == "/"
        # 第二次就该失败，防重放
        with pytest.raises(oidc.OIDCError):
            oidc.pop_state("abc")

    def test_unknown_state_rejected(self):
        from app.modules.auth import oidc

        with pytest.raises(oidc.OIDCError):
            oidc.pop_state("never-existed")

    def test_expired_state_rejected(self):
        import time

        from app.modules.auth import oidc

        with oidc._state_lock:
            oidc._states["old"] = {
                "created": time.time() - oidc._STATE_TTL - 10,
                "nonce": "n", "redirect_uri": "u", "next": "/",
            }
        with pytest.raises(oidc.OIDCError):
            oidc.pop_state("old")


class TestOidcTokenExchange:
    """客户端认证方式。选错会被提供商以 invalid_client 拒掉。"""

    INVALID_CLIENT = (
        '{"error":"invalid_client","error_description":'
        '"Client authentication failed."}'
    )

    @pytest.fixture
    def capture(self, settings, monkeypatch):
        """替换掉 discovery 与 httpx，记录每次 token 请求怎么带的凭据。"""
        import httpx

        from app.modules.auth import oidc

        monkeypatch.setattr(settings, "oidc_client_id", "cid", raising=False)
        monkeypatch.setattr(settings, "oidc_client_secret", "sec", raising=False)
        monkeypatch.setattr(settings, "proxy", "", raising=False)

        calls: list[dict] = []
        state = {"methods": None, "responses": []}

        def fake_discover():
            config = {
                "authorization_endpoint": "https://auth.test/authorize",
                "token_endpoint": "https://auth.test/token",
            }
            if state["methods"] is not None:
                config["token_endpoint_auth_methods_supported"] = state["methods"]
            return config

        monkeypatch.setattr(oidc, "discover", fake_discover)

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, data=None, auth=None, headers=None):
                calls.append({"data": dict(data or {}), "auth": auth})
                status, text = state["responses"][len(calls) - 1]
                return httpx.Response(
                    status, text=text, request=httpx.Request("POST", url)
                )

        monkeypatch.setattr(httpx, "Client", FakeClient)
        return calls, state

    def test_prefers_basic_when_post_unsupported(self, capture):
        """提供商只声明 basic，就该走 Authorization 头。"""
        from app.modules.auth import oidc

        calls, state = capture
        state["methods"] = ["client_secret_basic"]
        state["responses"] = [(200, '{"access_token":"tok"}')]

        assert oidc.exchange_code("code", "https://app.test/cb")["access_token"] == "tok"
        assert len(calls) == 1
        assert calls[0]["auth"] == ("cid", "sec")
        # 用了 basic 就不该再把 secret 塞进 body
        assert "client_secret" not in calls[0]["data"]

    def test_uses_post_when_declared(self, capture):
        from app.modules.auth import oidc

        calls, state = capture
        state["methods"] = ["client_secret_post"]
        state["responses"] = [(200, '{"id_token":"jwt"}')]

        oidc.exchange_code("code", "https://app.test/cb")
        assert calls[0]["auth"] is None
        assert calls[0]["data"]["client_secret"] == "sec"

    def test_defaults_to_basic_without_declaration(self, capture):
        """discovery 里没声明时按规范默认 client_secret_basic。"""
        from app.modules.auth import oidc

        calls, state = capture
        state["methods"] = None
        state["responses"] = [(200, '{"access_token":"tok"}')]

        oidc.exchange_code("code", "https://app.test/cb")
        assert calls[0]["auth"] == ("cid", "sec")

    def test_retries_with_other_method_on_invalid_client(self, capture):
        """声明与实际不符时，换另一种方式重试一次。"""
        from app.modules.auth import oidc

        calls, state = capture
        state["methods"] = ["client_secret_post"]
        state["responses"] = [
            (401, self.INVALID_CLIENT),
            (200, '{"access_token":"tok"}'),
        ]

        assert oidc.exchange_code("code", "https://app.test/cb")["access_token"] == "tok"
        assert len(calls) == 2
        assert calls[0]["auth"] is None
        assert calls[1]["auth"] == ("cid", "sec")

    def test_gives_up_after_both_methods_fail(self, capture):
        """两种都被拒就报错，不再重试。"""
        from app.modules.auth import oidc

        calls, state = capture
        state["methods"] = ["client_secret_basic"]
        state["responses"] = [
            (401, self.INVALID_CLIENT),
            (401, self.INVALID_CLIENT),
        ]

        with pytest.raises(oidc.OIDCError, match="401"):
            oidc.exchange_code("code", "https://app.test/cb")
        assert len(calls) == 2

    def test_other_errors_not_retried(self, capture):
        """invalid_grant 是授权码本身的问题，重试只会再失败一次。"""
        from app.modules.auth import oidc

        calls, state = capture
        state["methods"] = ["client_secret_basic"]
        state["responses"] = [(400, '{"error":"invalid_grant"}')]

        with pytest.raises(oidc.OIDCError):
            oidc.exchange_code("code", "https://app.test/cb")
        assert len(calls) == 1


class TestOidcUsername:
    def test_bind_username_wins(self, settings, monkeypatch):
        from app.modules.auth import oidc

        monkeypatch.setattr(settings, "oidc_bind_username", "admin", raising=False)
        assert oidc.resolve_username({"preferred_username": "someone"}) == "admin"

    def test_uses_configured_claim(self, settings, monkeypatch):
        from app.modules.auth import oidc

        monkeypatch.setattr(settings, "oidc_bind_username", "", raising=False)
        monkeypatch.setattr(settings, "oidc_username_claim", "email", raising=False)
        assert oidc.resolve_username({"email": "a@b.c", "sub": "x"}) == "a@b.c"

    def test_falls_back_to_sub(self, settings, monkeypatch):
        from app.modules.auth import oidc

        monkeypatch.setattr(settings, "oidc_bind_username", "", raising=False)
        monkeypatch.setattr(settings, "oidc_username_claim", "nope", raising=False)
        assert oidc.resolve_username({"sub": "user-123"}) == "user-123"

    def test_empty_userinfo_raises(self, settings, monkeypatch):
        from app.modules.auth import oidc

        monkeypatch.setattr(settings, "oidc_bind_username", "", raising=False)
        with pytest.raises(oidc.OIDCError):
            oidc.resolve_username({})


class TestPasskeyRpId:
    def test_configured_value_wins(self, settings, monkeypatch):
        from app.modules.auth import passkey

        monkeypatch.setattr(settings, "webauthn_rp_id", "example.com", raising=False)
        assert passkey._rp_id("https://other.test:8443") == "example.com"

    def test_derived_from_origin(self, settings, monkeypatch):
        from app.modules.auth import passkey

        monkeypatch.setattr(settings, "webauthn_rp_id", "", raising=False)
        # 端口不能带进 RP ID
        assert passkey._rp_id("https://lady.example.com:8443") == "lady.example.com"

    def test_invalid_origin_raises(self, settings, monkeypatch):
        from app.modules.auth import passkey

        monkeypatch.setattr(settings, "webauthn_rp_id", "", raising=False)
        with pytest.raises(passkey.PasskeyError):
            passkey._rp_id("not-a-url")


class TestPasskeyChallenge:
    def test_challenge_is_single_use(self):
        from app.modules.auth import passkey

        passkey._put_challenge("k", b"chal", rp_id="x")
        assert passkey._pop_challenge("k")["challenge"] == b"chal"
        with pytest.raises(passkey.PasskeyError):
            passkey._pop_challenge("k")

    def test_expired_challenge_rejected(self):
        import time

        from app.modules.auth import passkey

        passkey._put_challenge("old", b"c", rp_id="x")
        with passkey._lock:
            passkey._challenges["old"]["created"] = time.time() - passkey._CHALLENGE_TTL - 5
        with pytest.raises(passkey.PasskeyError):
            passkey._pop_challenge("old")


class TestPasskeyStorage:
    def _prepare(self):
        from app.database.base import DBBase
        from app.database.models import Passkey
        from app.database.session import engine, session_scope
        from sqlalchemy import select

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            for row in session.scalars(select(Passkey)).all():
                session.delete(row)

    def test_list_and_delete(self):
        from app.database.models import Passkey
        from app.database.session import session_scope
        from app.modules.auth import passkey

        self._prepare()
        with session_scope() as session:
            session.add(Passkey(
                credential_id="cred-1", username="admin",
                public_key="pk", sign_count=0, label="手机",
            ))

        items = passkey.list_credentials("admin")
        assert [i["label"] for i in items] == ["手机"]

        # 别人的凭证删不掉
        assert passkey.delete_credential("someone", "cred-1") is False
        assert passkey.delete_credential("admin", "cred-1") is True
        assert passkey.list_credentials("admin") == []

    def test_has_credentials(self):
        from app.database.models import Passkey
        from app.database.session import session_scope
        from app.modules.auth import passkey

        self._prepare()
        assert passkey.has_credentials() is False
        with session_scope() as session:
            session.add(Passkey(
                credential_id="c2", username="admin", public_key="pk",
            ))
        assert passkey.has_credentials() is True


class TestOriginResolution:
    """origin 算错是 SSO 与 Passkey 最常见的失败原因。

    容器内 nginx 监听明文 HTTP，转发的 X-Forwarded-Proto 是 http、
    Host 里也没有对外端口，直接用会得到 http://host 而不是
    https://host:8443。
    """

    def _request(self, headers, settings, monkeypatch, external=""):
        from starlette.datastructures import Headers
        from starlette.requests import Request

        from app.api.endpoints.auth import _origin

        monkeypatch.setattr(settings, "external_domain", external, raising=False)
        scope = {
            "type": "http",
            "scheme": "http",
            "headers": Headers(headers).raw,
            "method": "GET",
            "path": "/",
            "query_string": b"",
        }
        return _origin(Request(scope))

    def test_origin_header_wins_over_forwarded(self, settings, monkeypatch):
        """浏览器带的 Origin 最可靠，压过容器 nginx 的转发头。"""
        got = self._request(
            {
                "origin": "https://lady.example.com:8443",
                "host": "lady.example.com",
                "x-forwarded-proto": "http",
            },
            settings, monkeypatch,
        )
        assert got == "https://lady.example.com:8443"

    def test_falls_back_to_referer(self, settings, monkeypatch):
        got = self._request(
            {
                "referer": "https://lady.example.com:8443/config",
                "host": "lady.example.com",
                "x-forwarded-proto": "http",
            },
            settings, monkeypatch,
        )
        assert got == "https://lady.example.com:8443"

    def test_forwarded_port_is_appended(self, settings, monkeypatch):
        """Host 没带端口时用 X-Forwarded-Port 补。"""
        got = self._request(
            {
                "host": "lady.example.com",
                "x-forwarded-proto": "https",
                "x-forwarded-port": "8443",
            },
            settings, monkeypatch,
        )
        assert got == "https://lady.example.com:8443"

    def test_default_port_not_appended(self, settings, monkeypatch):
        """443 是 https 默认端口，加上反而对不上浏览器地址。"""
        got = self._request(
            {
                "host": "lady.example.com",
                "x-forwarded-proto": "https",
                "x-forwarded-port": "443",
            },
            settings, monkeypatch,
        )
        assert got == "https://lady.example.com"

    def test_multi_hop_forwarded_takes_first(self, settings, monkeypatch):
        """多级反代时 X-Forwarded-* 是逗号分隔的链。"""
        got = self._request(
            {
                "host": "lady.example.com",
                "x-forwarded-proto": "https, http",
                "x-forwarded-host": "lady.example.com, inner",
            },
            settings, monkeypatch,
        )
        assert got == "https://lady.example.com"

    def test_configured_external_domain_wins(self, settings, monkeypatch):
        got = self._request(
            {"origin": "https://other.test"},
            settings, monkeypatch,
            external="https://lady.example.com:8443",
        )
        assert got == "https://lady.example.com:8443"

    def test_external_domain_gets_https_prefix(self, settings, monkeypatch):
        got = self._request(
            {}, settings, monkeypatch, external="lady.example.com:8443",
        )
        assert got == "https://lady.example.com:8443"


class TestSsoAccountCannotUsePassword:
    """OIDC 建的账号密码为空，不能走密码登录。"""

    def test_empty_password_rejected(self, client):
        from app.database.models import User
        from app.database.session import session_scope

        with session_scope() as session:
            session.merge(User(username="ssouser", password=""))

        response = client.post(
            "/api/v1/login", json={"username": "ssouser", "password": ""}
        )
        assert response.json()["code"] == 401


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.api import create_app
    from app.database.base import DBBase
    from app.database.session import engine

    DBBase.metadata.create_all(engine)
    with TestClient(create_app()) as test_client:
        yield test_client


class TestAuthMethodsEndpoint:
    def test_public_and_no_secrets(self, client, monkeypatch):
        """登录页要读它，不能要求鉴权，也不能泄露密钥。"""
        response = client.get("/api/v1/auth/methods")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["password"] is True
        assert "client_secret" not in str(data)
