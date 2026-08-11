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
