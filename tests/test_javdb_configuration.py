from types import SimpleNamespace

from app.modules.ladysite import javdb
from app.modules.ladysite.base import SiteClient


def configured(monkeypatch, source_host='https://javdb.com', legacy='https://javdb.com'):
    client = SiteClient(source_host, 'source-session=test', interval=10, bypass_first=True)
    client.proxy = 'http://proxy.invalid:8080'
    monkeypatch.setattr(javdb, 'get_settings', lambda: SimpleNamespace(javdb_host=legacy))
    monkeypatch.setattr(SiteClient, 'from_source', lambda key: client)
    return client


def test_legacy_host_keeps_source_cookie_proxy_bypass_and_throttle(monkeypatch):
    client = configured(monkeypatch)
    result = javdb.Avdb().client
    assert result is client
    assert result.cookie == 'source-session=test'
    assert result.proxy == 'http://proxy.invalid:8080'
    assert result.bypass_first and result.interval == 10


def test_explicit_host_and_cookie_only_override_corresponding_fields(monkeypatch):
    client = configured(monkeypatch)
    result = javdb.Avdb('https://mirror.invalid', 'explicit-session=test').client
    assert result is client and result.host == 'https://mirror.invalid'
    assert result.cookie == 'explicit-session=test'
    assert result.bypass_first and result.interval == 10 and result.proxy


def test_source_mirror_is_not_hidden_by_default_legacy_host(monkeypatch):
    configured(monkeypatch, source_host='https://mirror.invalid')
    assert javdb.Avdb().client.host == 'https://mirror.invalid'


def test_custom_legacy_host_still_works_with_default_source(monkeypatch):
    configured(monkeypatch, legacy='https://legacy.invalid')
    assert javdb.Avdb().client.host == 'https://legacy.invalid'


def test_private_app_api_cannot_be_used_as_html_source(monkeypatch):
    configured(monkeypatch, source_host='https://apidd.example.invalid/api/v4')
    assert javdb.Avdb().client.host == javdb.DEFAULT_HOST
