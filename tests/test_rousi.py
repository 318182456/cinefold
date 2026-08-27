"""Rousi（站点后端 PeerGo）API 客户端测试。

站点在 2026 年换了一版接口，这里的载荷取自对线上的实测：
- 路径前缀 /api/v1，搜索是 GET /api/v1/torrents?query=&limit=&offset=
- Key 走 X-API-Key 头；用 Authorization: Bearer 会落到旧版兼容接口
- 返回 items/total 结构，字段是 name / size_bytes / promotion(字符串)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest

# 实测响应的精简版，字段名与线上一致
SEARCH_PAYLOAD = {
    "items": [
        {
            "id": 9204,
            "name": "ABP-554 絶対的鉄板シチュエーション 1",
            "subtitle": "中文字幕",
            "size_bytes": 26424115200,
            "seeders": 27,
            "leechers": 2,
            "completed": 10,
            "promotion": "none",
            "category": {"id": "9kg", "name": "9KG"},
        },
        {
            "id": 9539,
            "name": "SSIS-637 4K",
            "subtitle": "简体中文硬字幕",
            "size_bytes": 5488373760,
            "seeders": 107,
            "promotion": "double_upload_free",
            "category": {"id": "9kg", "name": "9KG"},
        },
    ],
    "total": 2,
    "limit": 50,
    "offset": 0,
}

# 用 Authorization: Bearer 时线上返回的形状 —— 忽略全部查询参数
LEGACY_PAYLOAD = {
    "code": 0,
    "message": "success",
    "data": {
        "page": 1,
        "page_size": 100,
        "torrents": [{"id": 8448, "title": "无关种子"}],
    },
}


@pytest.fixture
def site():
    from app.modules.ptsite.rousi import Rousi
    return Rousi(apikey="pgk_test", host="https://rousi.pro")


class TestConfig:
    def test_disabled_without_apikey(self):
        from app.modules.ptsite.rousi import Rousi
        assert Rousi(apikey="").enabled is False
        assert Rousi(apikey="").search("ABP-984") == []

    def test_enabled_with_apikey(self):
        from app.modules.ptsite.rousi import Rousi
        assert Rousi(apikey="pgk_x").enabled is True

    def test_apikey_whitespace_stripped(self):
        """从网页复制 Key 很容易带上首尾空白，带进 header 会鉴权失败。"""
        from app.modules.ptsite.rousi import Rousi
        assert Rousi(apikey="  pgk_x\n").apikey == "pgk_x"

    def test_host_override(self, monkeypatch):
        from app.modules.ptsite.rousi import Rousi

        assert Rousi(apikey="t").host == "https://rousi.pro"
        monkeypatch.setenv("ROUSI_HOST", "https://new.example.com/")
        assert Rousi(apikey="t").host == "https://new.example.com"

    def test_uses_x_api_key_header(self, site):
        """必须是 X-API-Key。Authorization: Bearer 会落到旧接口且搜索失效。"""
        headers = site._headers()
        assert headers["X-API-Key"] == "pgk_test"
        assert "Authorization" not in headers


class TestSearch:
    def _patch(self, monkeypatch, payload, captured=None, url_seen=None):
        def fake_get(self, url, **kwargs):
            if captured is not None:
                captured.update(kwargs.get("params") or {})
            if url_seen is not None:
                url_seen.append(str(url))
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))
        monkeypatch.setattr(httpx.Client, "get", fake_get)

    def test_hits_v1_torrents_endpoint(self, site, monkeypatch):
        """旧的 /api/torrent/search 已 404，必须打 /api/v1/torrents。"""
        seen = []
        self._patch(monkeypatch, SEARCH_PAYLOAD, url_seen=seen)
        site.search("ABP-554")
        assert seen[0] == "https://rousi.pro/api/v1/torrents"

    def test_queries_with_full_code_and_limit(self, site, monkeypatch):
        """参数是 query/limit/offset —— 旧实现传的 page 已不被识别。"""
        captured = {}
        self._patch(monkeypatch, SEARCH_PAYLOAD, captured)
        site.search("abp554")
        assert captured["query"] == "ABP-554"
        assert captured["offset"] == 0
        assert "page" not in captured

    def test_parses_items(self, site, monkeypatch):
        self._patch(monkeypatch, SEARCH_PAYLOAD)
        results = site.search("ABP-554")
        assert len(results) == 2
        assert results[0].id == 9204
        assert results[0].seeders == 27

    def test_size_from_size_bytes(self, site, monkeypatch):
        """字段改名了：size 变成 size_bytes，读错会全变成 0。"""
        self._patch(monkeypatch, SEARCH_PAYLOAD)
        assert 25000 < site.search("ABP-554")[0].size_mb < 25500

    def test_promotion_string_free_detection(self, site, monkeypatch):
        """promotion 从对象变成字符串，double_upload_free 也算免费。"""
        self._patch(monkeypatch, SEARCH_PAYLOAD)
        results = site.search("ABP-554")
        assert results[0].free is False
        assert results[1].free is True

    def test_chinese_detected_from_subtitle(self, site, monkeypatch):
        """主标题常是纯日文，中文信息在副标题里，判定要合起来看。"""
        self._patch(monkeypatch, SEARCH_PAYLOAD)
        assert site.search("ABP-554")[0].chinese is True

    def test_legacy_shape_treated_as_failure(self, site, monkeypatch):
        """落到旧接口时返回全站第一页，绝不能当成搜索命中。

        这是换接口后最阴的坑：HTTP 200、code 0，但查询参数被忽略，
        每个番号都会「搜到」一堆无关种子。
        """
        self._patch(monkeypatch, LEGACY_PAYLOAD)
        assert site.search("ABP-554") == []
        assert site.search_failed is True

    def test_error_status_returns_empty(self, site, monkeypatch):
        def fake_get(self, url, **kwargs):
            return httpx.Response(401, json={"code": 401},
                                  request=httpx.Request("GET", url))
        monkeypatch.setattr(httpx.Client, "get", fake_get)
        assert site.search("ABP-554") == []
        assert site.search_failed is True

    def test_network_error_returns_empty(self, site, monkeypatch):
        def boom(self, url, **kwargs):
            raise httpx.ConnectError("refused")
        monkeypatch.setattr(httpx.Client, "get", boom)
        assert site.search("ABP-554") == []
        assert site.search_failed is True


class TestCheckStatus:
    def test_ok_with_items_shape(self, site, monkeypatch):
        def fake_get(self, url, **kwargs):
            return httpx.Response(200, json={"items": [], "total": 8774},
                                  request=httpx.Request("GET", url))
        monkeypatch.setattr(httpx.Client, "get", fake_get)
        ok, msg = site.check_status()
        assert ok is True
        assert "8774" in msg

    def test_legacy_shape_reported(self, site, monkeypatch):
        """命中旧接口要说清楚，否则用户以为配好了但搜索全是错的。"""
        def fake_get(self, url, **kwargs):
            return httpx.Response(200, json=LEGACY_PAYLOAD,
                                  request=httpx.Request("GET", url))
        monkeypatch.setattr(httpx.Client, "get", fake_get)
        ok, msg = site.check_status()
        assert ok is False
        assert "旧版接口" in msg

    def test_invalid_key(self, site, monkeypatch):
        def fake_get(self, url, **kwargs):
            return httpx.Response(401, json={}, request=httpx.Request("GET", url))
        monkeypatch.setattr(httpx.Client, "get", fake_get)
        ok, msg = site.check_status()
        assert ok is False
        assert "无效" in msg

    def test_no_apikey(self):
        from app.modules.ptsite.rousi import Rousi
        ok, msg = Rousi(apikey="").check_status()
        assert ok is False
        assert "API Key" in msg


class TestDownloadUrl:
    def test_empty_without_apikey(self):
        from app.modules.ptsite.rousi import Rousi
        assert Rousi(apikey="")._build_download_url(9204) == ""

    def test_points_at_v1_download(self, site):
        url = site._build_download_url(9204)
        assert url == "https://rousi.pro/api/v1/torrents/9204/download"

    def test_magnet_not_downloaded_as_file(self):
        from app.modules.ptsite.rousi import Rousi
        from app.schemas.torrent import Torrent

        site = Rousi(apikey="pgk_x")
        torrent = Torrent(download_url="magnet:?xt=urn:btih:x")
        assert site.download_seed(torrent) is None
