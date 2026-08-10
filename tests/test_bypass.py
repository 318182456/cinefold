"""bypass 服务调用与站点地址校正测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest


class TestHostResolve:
    def test_api_host_falls_back_to_official(self):
        """填成 App 接口域名时应回退，否则 HTML 解析必然失败。"""
        from app.modules.ladysite.javdb import _resolve_host, DEFAULT_HOST

        assert _resolve_host("https://apidd.czssdgz.com") == DEFAULT_HOST
        assert _resolve_host("https://x.com/api/v2") == DEFAULT_HOST

    def test_normal_host_kept(self):
        from app.modules.ladysite.javdb import _resolve_host

        assert _resolve_host("https://javdb.com") == "https://javdb.com"
        assert _resolve_host("https://javdb456.com/") == "https://javdb456.com"

    def test_empty_host_uses_default(self):
        from app.modules.ladysite.javdb import _resolve_host, DEFAULT_HOST

        assert _resolve_host("") == DEFAULT_HOST


class TestBypass:
    def test_returns_empty_when_not_configured(self, monkeypatch):
        from app.core import config as config_module
        from app.modules.ladysite.base import fetch_via_bypass

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "bypass_url", "")
        assert fetch_via_bypass("https://javdb.com") == ""

    def test_flaresolverr_response_parsed(self, monkeypatch):
        from app.core import config as config_module
        from app.modules.ladysite import base

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "bypass_url", "http://127.0.0.1:8191/v1")

        def fake_post(self, url, **kwargs):
            assert kwargs["json"]["cmd"] == "request.get"
            return httpx.Response(
                200,
                json={"status": "ok", "solution": {"response": "<html>OK</html>"}},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        assert base.fetch_via_bypass("https://javdb.com") == "<html>OK</html>"

    def test_flaresolverr_failure_returns_empty(self, monkeypatch):
        from app.core import config as config_module
        from app.modules.ladysite import base

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "bypass_url", "http://127.0.0.1:8191/v1")

        def fake_post(self, url, **kwargs):
            return httpx.Response(
                200,
                json={"status": "error", "message": "challenge failed"},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        assert base.fetch_via_bypass("https://javdb.com") == ""

    def test_scraper_style_service(self, monkeypatch):
        """非 FlareSolverr 服务走 GET /html?url=..."""
        from app.core import config as config_module
        from app.modules.ladysite import base

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "bypass_url", "http://127.0.0.1:8000")
        # 跳过类型探测，直接走 scraper 分支
        monkeypatch.setattr(base, "_is_flaresolverr", lambda base_url: False)

        captured = {}

        def fake_get(self, url, **kwargs):
            captured["url"] = url
            captured["params"] = kwargs.get("params")
            return httpx.Response(200, text="<html>SCRAPED</html>",
                                  request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        result = base.fetch_via_bypass("https://javdb.com/search", {"q": "ABP-984"})

        assert result == "<html>SCRAPED</html>"
        assert captured["url"].endswith("/html")
        # params 应被并进目标 URL
        assert "q=ABP-984" in captured["params"]["url"]

    def test_params_merged_into_url(self, monkeypatch):
        from app.core import config as config_module
        from app.modules.ladysite import base

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "bypass_url", "http://127.0.0.1:8191/v1")
        captured = {}

        def fake_post(self, url, **kwargs):
            captured["target"] = kwargs["json"]["url"]
            return httpx.Response(
                200, json={"status": "ok", "solution": {"response": "x"}},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        base.fetch_via_bypass("https://javdb.com/search", {"q": "SSIS-001", "f": ""})

        assert "q=SSIS-001" in captured["target"]
        # 空值参数应被剔除
        assert "f=" not in captured["target"]

    def test_solve_timeout_is_generous(self, monkeypatch):
        """过盾要跑真实浏览器，不能沿用调用方的短超时。"""
        from app.core import config as config_module
        from app.modules.ladysite import base

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "bypass_url", "http://127.0.0.1:8191/v1")
        captured = {}

        def fake_post(self, url, **kwargs):
            captured["max_timeout"] = kwargs["json"]["maxTimeout"]
            return httpx.Response(
                200, json={"status": "ok", "solution": {"response": "x"}},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        # 即使调用方只给 15 秒，传给 solver 的也应是宽松值
        base.fetch_via_bypass("https://javdb.com", timeout=15.0)
        assert captured["max_timeout"] >= 60000

    def test_requests_are_serialized(self, monkeypatch):
        """FlareSolverr 并发会互相拖慢，同一时刻只应有一个请求在途。"""
        import threading
        import time

        from app.core import config as config_module
        from app.modules.ladysite import base

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "bypass_url", "http://127.0.0.1:8191/v1")

        in_flight = 0
        max_in_flight = 0
        guard = threading.Lock()

        def fake_post(self, url, **kwargs):
            nonlocal in_flight, max_in_flight
            with guard:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            time.sleep(0.05)
            with guard:
                in_flight -= 1
            return httpx.Response(
                200, json={"status": "ok", "solution": {"response": "x"}},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx.Client, "post", fake_post)

        threads = [
            threading.Thread(target=base.fetch_via_bypass, args=("https://javdb.com",))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert max_in_flight == 1

    def test_network_error_returns_empty(self, monkeypatch):
        from app.core import config as config_module
        from app.modules.ladysite import base

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "bypass_url", "http://127.0.0.1:8191/v1")

        def boom(self, url, **kwargs):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx.Client, "post", boom)
        assert base.fetch_via_bypass("https://javdb.com") == ""


class TestSiteClientFallback:
    def test_403_triggers_bypass(self, monkeypatch):
        """直连 403 时应自动改走 bypass。"""
        from app.modules.ladysite import base

        client = base.SiteClient("https://javdb.com", interval=0)

        def fake_get(self, url, **kwargs):
            return httpx.Response(403, text="denied", request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        monkeypatch.setattr(base, "fetch_via_bypass",
                            lambda url, params=None, timeout=15.0: "<html>VIA</html>")

        assert client.get("/search") == "<html>VIA</html>"

    def test_other_errors_do_not_trigger_bypass(self, monkeypatch):
        from app.modules.ladysite import base

        client = base.SiteClient("https://javdb.com", interval=0)
        called = {"bypass": False}

        def fake_get(self, url, **kwargs):
            return httpx.Response(500, text="err", request=httpx.Request("GET", url))

        def spy(url, params=None, timeout=15.0):
            called["bypass"] = True
            return "should not happen"

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        monkeypatch.setattr(base, "fetch_via_bypass", spy)

        assert client.get("/search") == ""
        assert called["bypass"] is False
