import httpx
import pytest

from app.core.config import Settings
from app.modules.ptsite import nexus


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setattr(nexus, 'get_settings', lambda: Settings())
    nexus.NexusSite.reset_breakers()
    yield nexus.NexusSite(cookie='session=private', host='https://site.test')
    nexus.NexusSite.reset_breakers()


def mock_site(monkeypatch, status=200, html='<table class="torrents"></table>', *, redirect=False):
    requests = []
    def handle(request):
        requests.append(request)
        if redirect and request.url.path != '/login.php':
            return httpx.Response(302, headers={'Location': '/login.php'})
        return httpx.Response(status, text=html, headers={'Server': 'cloudflare'})
    factory = httpx.Client
    monkeypatch.setattr(nexus.httpx, 'Client', lambda **kw: factory(transport=httpx.MockTransport(handle), **kw))
    return requests


def test_checks_actual_search_resource_instead_of_index(site, monkeypatch):
    requests = mock_site(monkeypatch)
    ok, message = site.check_status()
    assert ok and requests[0].url.path == '/torrents.php'
    assert requests[0].headers['Cookie'] == 'session=private'
    assert len(requests) == 1


@pytest.mark.parametrize('status,html,expected', [
    (409, '<title>DNS resolution error | Cloudflare</title>', 'Cloudflare 1001'),
    (409, '<html>Conflict</html>', 'HTTP 409'),
    (404, '<title>404 Not Found</title>', 'HTTP 404'),
    (403, '<title>Forbidden</title>', 'HTTP 403'),
    (401, '', '更新 Cookie'),
    (429, '', '访问频率'),
    (200, '<title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/a"></script>', '浏览器验证'),
    (200, '<form action="takelogin.php"><input type="password"></form>', '未登录'),
    (200, '<html>Welcome to nginx</html>', '未识别'),
])
def test_reports_specific_failure_and_does_not_claim_connected(site, monkeypatch, status, html, expected):
    mock_site(monkeypatch, status, html)
    ok, message = site.check_status()
    assert not ok and expected in message
    assert 'private' not in message and 'developer.mozilla' not in message


def test_redirect_to_login_is_not_success_even_without_password_form(site, monkeypatch):
    mock_site(monkeypatch, html='<html>Please sign in</html>', redirect=True)
    assert 'Cookie' in site.check_status()[1]
    assert not site.check_status()[0]


def test_logged_in_chinese_header_is_not_login_failure(site, monkeypatch):
    mock_site(monkeypatch, html='<html><p>上次登录：今天</p><table class="torrents"></table></html>')
    assert site.check_status()[0]


def test_search_uses_same_error_diagnosis_and_marks_failure(site, monkeypatch):
    mock_site(monkeypatch, 409, '<title>DNS resolution error | Cloudflare</title>')
    assert site.search('SONE-725') == []
    assert site.search_failed and site._breaker()[0] == 1
