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
                            lambda url, params=None, timeout=15.0, **kw: "<html>VIA</html>")

        assert client.get("/search") == "<html>VIA</html>"

    def test_5xx_triggers_bypass(self, monkeypatch):
        """5xx 多半是站点前面的防护层掐了直连，值得让 bypass 再试一次。"""
        from app.modules.ladysite import base

        client = base.SiteClient("https://javdb.com", interval=0)

        def fake_get(self, url, **kwargs):
            return httpx.Response(502, text="bad gateway", request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        monkeypatch.setattr(base, "fetch_via_bypass",
                            lambda url, params=None, timeout=15.0, **kw: "<html>VIA</html>")

        assert client.get("/search") == "<html>VIA</html>"

    def test_other_errors_do_not_trigger_bypass(self, monkeypatch):
        """4xx（除 403）是站点的真实答复，过盾也变不出页面来，不该白跑一趟。"""
        from app.modules.ladysite import base

        client = base.SiteClient("https://javdb.com", interval=0)
        called = {"bypass": False}

        def fake_get(self, url, **kwargs):
            return httpx.Response(404, text="not found", request=httpx.Request("GET", url))

        def spy(url, params=None, timeout=15.0):
            called["bypass"] = True
            return "should not happen"

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        monkeypatch.setattr(base, "fetch_via_bypass", spy)

        assert client.get("/search") == ""
        assert called["bypass"] is False

    def test_quick_mode_uses_shorter_timeout(self, monkeypatch):
        """连通性测试有人在页面上等，且过盾请求串行，不能用 90s。"""
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
        base.fetch_via_bypass("https://javdb.com", quick=True)
        # 要留在前端 60s 超时以内
        assert captured["max_timeout"] < 60000


class TestCheckSource:
    """测试按钮必须和抓取链路走同一条路，否则报的"不通"是假的。"""

    @pytest.fixture(autouse=True)
    def _no_db_write(self, monkeypatch):
        """check_source 末尾要写库，测试里不关心，直接短路。"""
        import contextlib

        from app.modules.ladysite import sources

        @contextlib.contextmanager
        def fake_scope():
            class _Session:
                def scalar(self, *a, **kw):
                    return None
            yield _Session()

        monkeypatch.setattr(sources, "session_scope", fake_scope)

    def test_bypass_first_source_skips_direct(self, monkeypatch):
        """javlibrary 这类站直连必 403，配了过盾就该直接走过盾。"""
        from app.core import config as config_module
        from app.modules.ladysite import base, sources

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "bypass_url", "http://127.0.0.1:8191/v1")
        monkeypatch.setattr(sources, "get_source", lambda key: {
            "key": key, "host": "https://www.javlibrary.com/cn",
            "enabled": True, "bypass_first": True,
        })

        def no_direct(self, url, **kwargs):
            raise AssertionError("bypass_first 的源不该直连")

        monkeypatch.setattr(httpx.Client, "get", no_direct)
        monkeypatch.setattr(base, "fetch_via_bypass",
                            lambda url, params=None, timeout=60.0, quick=False: "<html>real</html>")

        result = sources.check_source("javlibrary")
        assert result["status"] == "ok"

    def test_direct_403_retries_via_bypass(self, monkeypatch):
        """直连被拦时抓取链路会改走过盾，测试也要跟上。"""
        from app.core import config as config_module
        from app.modules.ladysite import base, sources

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "bypass_url", "http://127.0.0.1:8191/v1")
        monkeypatch.setattr(sources, "get_source", lambda key: {
            "key": key, "host": "https://javdb.com",
            "enabled": True, "bypass_first": False,
        })
        monkeypatch.setattr(httpx.Client, "get", lambda self, url, **kw: httpx.Response(
            403, text="denied", request=httpx.Request("GET", url)))
        monkeypatch.setattr(base, "fetch_via_bypass",
                            lambda url, params=None, timeout=60.0, quick=False: "<html>real</html>")

        result = sources.check_source("javdb")
        assert result["status"] == "ok"

    def test_no_bypass_configured_says_so(self, monkeypatch):
        """没配过盾服务时才该提示去配，不能反过来污蔑服务没运行。"""
        from app.core import config as config_module
        from app.modules.ladysite import sources

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "bypass_url", "")
        monkeypatch.setattr(sources, "get_source", lambda key: {
            "key": key, "host": "https://www.javlibrary.com/cn",
            "enabled": True, "bypass_first": True,
        })
        monkeypatch.setattr(httpx.Client, "get", lambda self, url, **kw: httpx.Response(
            403, text="denied", request=httpx.Request("GET", url)))

        result = sources.check_source("javlibrary")
        assert result["status"] == "blocked"
        assert "需配置反爬绕过服务" in result["message"]

    def test_bypass_returning_verify_page_is_blocked(self, monkeypatch):
        """过盾服务可能拿回 200 的校验页，那不算通。"""
        from app.core import config as config_module
        from app.modules.ladysite import base, sources

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "bypass_url", "http://127.0.0.1:8191/v1")
        monkeypatch.setattr(sources, "get_source", lambda key: {
            "key": key, "host": "https://missav123.com",
            "enabled": True, "bypass_first": True,
        })
        monkeypatch.setattr(
            base, "fetch_via_bypass",
            lambda url, params=None, timeout=60.0, quick=False: "<html>age-check</html>")

        assert sources.check_source("missav")["status"] == "blocked"

    def test_javbus_probes_detail_page(self, monkeypatch):
        """javbus 首页固定跳 driver-verify，测首页等于永远不可用。"""
        from app.modules.ladysite import sources

        captured = {}

        def fake_get(self, url, **kwargs):
            captured["url"] = url
            return httpx.Response(200, text="ok", request=httpx.Request("GET", url))

        monkeypatch.setattr(sources, "get_source", lambda key: {
            "key": key, "host": "https://www.javbus.com",
            "enabled": True, "bypass_first": False,
        })
        monkeypatch.setattr(httpx.Client, "get", fake_get)

        result = sources.check_source("javbus")
        assert result["status"] == "ok"
        assert captured["url"] != "https://www.javbus.com"

    def test_detail_probe_not_reported_as_redirect(self, monkeypatch):
        """探针路径不能被当成"跳转"，那是正常落地。"""
        from app.modules.ladysite import sources

        monkeypatch.setattr(sources, "get_source", lambda key: {
            "key": key, "host": "https://www.javbus.com",
            "enabled": True, "bypass_first": False,
        })
        monkeypatch.setattr(httpx.Client, "get", lambda self, url, **kw: httpx.Response(
            200, text="ok", request=httpx.Request("GET", url)))

        assert sources.check_source("javbus")["message"] == ""

    def test_direct_check_sends_full_headers(self, monkeypatch):
        """直连测试必须发全套头。

        javbus 只认到 User-Agent 时会把请求当机器人，一律 302 到
        /doc/driver-verify，于是测试报"未真正进站"而实际抓取是好的。
        Accept-Language 是这里的决定性字段。
        """
        from app.modules.ladysite import sources

        captured = {}

        def fake_get(self, url, **kwargs):
            captured.update(kwargs.get("headers") or {})
            return httpx.Response(200, text="ok", request=httpx.Request("GET", url))

        monkeypatch.setattr(sources, "get_source", lambda key: {
            "key": key, "host": "https://www.javbus.com",
            "enabled": True, "bypass_first": False, "cookie": "", "interval": 0.0,
        })
        monkeypatch.setattr(httpx.Client, "get", fake_get)

        assert sources.check_source("javbus")["status"] == "ok"
        assert captured.get("Accept-Language")
        assert captured.get("Accept")
        assert "Chrome" in captured.get("User-Agent", "")

    def test_direct_check_sends_source_cookie(self, monkeypatch):
        """页面上给源配的 Cookie 也要带上，否则测的不是用户那套配置。"""
        from app.modules.ladysite import sources

        captured = {}

        def fake_get(self, url, **kwargs):
            captured.update(kwargs.get("headers") or {})
            return httpx.Response(200, text="ok", request=httpx.Request("GET", url))

        monkeypatch.setattr(sources, "get_source", lambda key: {
            "key": key, "host": "https://javdb.com",
            "enabled": True, "bypass_first": False,
            "cookie": "over18=1", "interval": 0.0,
        })
        monkeypatch.setattr(httpx.Client, "get", fake_get)

        assert sources.check_source("javdb")["status"] == "ok"
        assert captured.get("Cookie") == "over18=1"
