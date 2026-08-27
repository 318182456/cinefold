"""Rousi API 客户端测试。

新站是前后端分离架构，与 NexusPHP 系站点行为不同，且只认个人 API Key ——
账号密码登录、JWT 续期、Tracker Passkey 都已去掉。
"""
import sys
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
    return Rousi(apikey="AKEY", host="https://rousi.pro")


class TestConfig:
    def test_disabled_without_apikey(self):
        from app.modules.ptsite.rousi import Rousi
        assert Rousi(apikey="").enabled is False
        assert Rousi(apikey="").search("ABP-984") == []

    def test_enabled_with_apikey(self):
        from app.modules.ptsite.rousi import Rousi
        assert Rousi(apikey="AKEY").enabled is True

    def test_apikey_whitespace_stripped(self):
        """从网页复制 Key 很容易带上首尾空白，带进 header 会鉴权失败。"""
        from app.modules.ptsite.rousi import Rousi
        assert Rousi(apikey="  AKEY\n").apikey == "AKEY"

    def test_host_override(self, monkeypatch):
        from app.modules.ptsite.rousi import Rousi

        assert Rousi(apikey="t").host == "https://rousi.pro"
        monkeypatch.setenv("ROUSI_HOST", "https://new.example.com/")
        assert Rousi(apikey="t").host == "https://new.example.com"

    def test_bearer_header(self, site):
        assert site._headers()["Authorization"] == "Bearer AKEY"


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


class TestDownloadUrl:
    def test_apikey_goes_in_path_not_query(self, site):
        """站点的上游下载协议把 Key 放在路径里，不是查询参数。"""
        url = site._build_download_url(9204, "abc")
        assert url == "https://rousi.pro/api/torrent/download/AKEY/9204"
        assert "passkey=" not in url
        assert "?" not in url

    def test_falls_back_to_magnet_without_apikey(self):
        from app.modules.ptsite.rousi import Rousi

        site = Rousi(apikey="")
        url = site._build_download_url(9204, "aabbcc")
        assert url == "magnet:?xt=urn:btih:aabbcc"

    def test_empty_without_apikey_and_hash(self):
        from app.modules.ptsite.rousi import Rousi
        assert Rousi(apikey="")._build_download_url(9204, "") == ""

    def test_magnet_not_downloaded_as_file(self):
        from app.modules.ptsite.rousi import Rousi
        from app.schemas.torrent import Torrent

        site = Rousi(apikey="")
        torrent = Torrent(download_url="magnet:?xt=urn:btih:x")
        assert site.download_seed(torrent) is None
