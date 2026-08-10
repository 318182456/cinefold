"""Rousi API 客户端测试。

新站是前后端分离架构，与 NexusPHP 系站点行为不同。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest

SEARCH_PAYLOAD = {
    "code": 0,
    "message": "success",
    "data": {
        "total": 2,
        "torrents": [
            {
                "id": 9204,
                "name": "ABP-554 絶対的鉄板シチュエーション 1",
                "size": 26424115200,
                "seeders": 27,
                "leechers": 2,
                "info_hash": "aabbccddeeff00112233445566778899aabbccdd",
                "price": 5000,
                "promotion": {},
                "attributes": {"resolution": "1080p"},
            },
            {
                "id": 9539,
                "name": "SSIS-637 中文字幕 4K",
                "size": 5488373760,
                "seeders": 107,
                "info_hash": "1122334455667788990011223344556677889900",
                "price": 0,
                "promotion": {"type": "free"},
                "attributes": {"resolution": "2160p"},
            },
        ],
    },
}


@pytest.fixture
def site():
    from app.modules.ptsite.rousi import Rousi
    return Rousi(token="tok", passkey="pk", host="https://rousi.pro")


def _jwt(exp: float) -> str:
    """构造一个只带 exp 的假 JWT。"""
    import base64
    import json

    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.sig"


class TestConfig:
    def test_disabled_without_credentials(self):
        from app.modules.ptsite.rousi import Rousi
        assert Rousi(token="").enabled is False
        assert Rousi(token="").search("ABP-984") == []

    def test_enabled_with_username_password(self):
        from app.modules.ptsite.rousi import Rousi
        assert Rousi(token="", username="u", password="p").enabled is True

    def test_host_override(self, monkeypatch):
        from app.modules.ptsite.rousi import Rousi

        assert Rousi(token="t").host == "https://rousi.pro"
        monkeypatch.setenv("ROUSI_HOST", "https://new.example.com/")
        assert Rousi(token="t").host == "https://new.example.com"

    def test_bearer_header(self, site):
        assert site._headers()["Authorization"] == "Bearer tok"


class TestSearch:
    def _patch(self, monkeypatch, payload, captured=None):
        def fake_get(self, url, **kwargs):
            if captured is not None:
                captured.update(kwargs.get("params") or {})
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))
        monkeypatch.setattr(httpx.Client, "get", fake_get)

    def test_queries_with_full_code(self, site, monkeypatch):
        """站点支持带横杠的完整番号，应原样查询。"""
        captured = {}
        self._patch(monkeypatch, SEARCH_PAYLOAD, captured)
        site.search("abp554")
        assert captured["query"] == "ABP-554"

    def test_parses_all_returned_items(self, site, monkeypatch):
        self._patch(monkeypatch, SEARCH_PAYLOAD)
        results = site.search("ABP-554")
        assert len(results) == 2
        assert results[0].id == 9204
        assert results[0].seeders == 27

    def test_size_converted_from_bytes(self, site, monkeypatch):
        self._patch(monkeypatch, SEARCH_PAYLOAD)
        result = site.search("ABP-554")[0]
        assert 25000 < result.size_mb < 25500

    def test_free_and_attribute_detection(self, site, monkeypatch):
        self._patch(monkeypatch, SEARCH_PAYLOAD)
        result = site.search("SSIS-637")[1]
        assert result.free is True
        assert result.chinese is True
        assert result.uhd is True

    def test_error_code_returns_empty(self, site, monkeypatch):
        self._patch(monkeypatch, {"code": 101, "message": "登录状态无效"})
        assert site.search("ABP-554") == []

    def test_network_error_returns_empty(self, site, monkeypatch):
        def boom(self, url, **kwargs):
            raise httpx.ConnectError("refused")
        monkeypatch.setattr(httpx.Client, "get", boom)
        assert site.search("ABP-554") == []


class TestAutoLogin:
    def test_valid_token_not_refreshed(self, monkeypatch):
        """token 还没到期就不该触发登录。"""
        from app.modules.ptsite.rousi import Rousi

        called = {"login": False}

        def spy(self):
            called["login"] = True
            return "new"

        monkeypatch.setattr(Rousi, "_login", spy)
        site = Rousi(token=_jwt(time.time() + 86400), username="u", password="p")
        assert site.token.startswith("header.")
        assert called["login"] is False

    def test_expired_token_triggers_login(self, monkeypatch):
        from app.modules.ptsite.rousi import Rousi

        monkeypatch.setattr(Rousi, "_login", lambda self: "fresh-token")
        site = Rousi(token=_jwt(time.time() - 10), username="u", password="p")
        assert site.token == "fresh-token"

    def test_no_credentials_keeps_stale_token(self, monkeypatch):
        """只配了 token 时不做登录，原样返回让调用方看到 401。"""
        from app.modules.ptsite.rousi import Rousi

        stale = _jwt(time.time() - 10)
        site = Rousi(token=stale)
        assert site.token == stale

    def test_login_extracts_token(self, monkeypatch):
        from app.modules.ptsite.rousi import Rousi

        def fake_post(self, url, **kwargs):
            assert kwargs["json"]["identifier"] == "u"
            return httpx.Response(
                200,
                json={"code": 0, "data": {"token": "tok-from-login"}},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        site = Rousi(token="", username="u", password="p")
        assert site.token == "tok-from-login"

    def test_login_failure_returns_empty(self, monkeypatch):
        from app.modules.ptsite.rousi import Rousi

        def fake_post(self, url, **kwargs):
            return httpx.Response(
                401, json={"code": 102, "message": "用户名或密码错误"},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        assert Rousi(token="", username="u", password="bad").token == ""

    def test_malformed_token_treated_as_valid(self):
        """非 JWT 格式的 token 无法判断有效期，不应反复触发登录。"""
        from app.modules.ptsite.rousi import Rousi

        assert Rousi._is_expiring("not-a-jwt") is False


class TestDownloadUrl:
    def test_uses_passkey_when_available(self, site):
        url = site._build_download_url(9204, "abc")
        assert "/api/torrent/9204/download" in url
        assert "passkey=pk" in url

    def test_falls_back_to_magnet(self):
        from app.modules.ptsite.rousi import Rousi

        site = Rousi(token="t", passkey="")
        url = site._build_download_url(9204, "aabbcc")
        assert url == "magnet:?xt=urn:btih:aabbcc"

    def test_magnet_not_downloaded_as_file(self):
        from app.modules.ptsite.rousi import Rousi
        from app.schemas.torrent import Torrent

        site = Rousi(token="t", passkey="")
        torrent = Torrent(download_url="magnet:?xt=urn:btih:x")
        assert site.download_seed(torrent) is None
