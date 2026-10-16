"""Telegram 代理建连重试、重复发送保护与错误脱敏。"""
from types import SimpleNamespace

import httpx
import pytest

from app.modules.notify import telegram


@pytest.fixture
def notifier(monkeypatch):
    monkeypatch.setattr(telegram, 'get_settings', lambda: SimpleNamespace(
        telegram_bot_token='123:private-token', telegram_chat_id='456', telegram_spoiler=False,
        proxy='socks5://user:password@proxy.test:1080'))
    monkeypatch.setattr(telegram.time, 'sleep', lambda _: None)
    return telegram.TelegramNotifier()


def install_transport(monkeypatch, handler):
    options = []
    def factory(**kwargs):
        options.append(kwargs)
        return httpx.MockTransport(handler)
    monkeypatch.setattr(telegram.httpx, 'HTTPTransport', factory)
    return options


def test_socks_tls_connect_failure_retries_once_before_sending(notifier, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError('[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]', request=request)
        return httpx.Response(200, json={'ok': True})
    options = install_transport(monkeypatch, handler)
    assert notifier.send_text_message('test')
    assert len(calls) == 2
    assert options[0]['proxy'] == notifier.proxy
    assert options[0]['trust_env'] is False
    assert options[0].get('verify', True) is True


@pytest.mark.parametrize('failure', [httpx.ReadTimeout, httpx.WriteError, httpx.ReadError])
def test_failures_after_connection_do_not_retry_message(notifier, monkeypatch, failure):
    calls = []
    def handler(request):
        calls.append(request)
        raise failure('connection interrupted', request=request)
    install_transport(monkeypatch, handler)
    assert not notifier.send_text_message('test')
    assert len(calls) == 1


def test_persistent_tls_failure_is_bounded_and_connection_test_redacts_token(notifier, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectError(f'SSL handshake failed for {request.url}', request=request)
    install_transport(monkeypatch, handler)
    ok, message = notifier.test_connection()
    assert not ok and len(calls) == 2
    assert 'TLS' in message and '代理' in message
    assert notifier.token not in message


def test_http_error_does_not_retry_and_redacts_secrets(notifier, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(403, json={'ok': False})
    install_transport(monkeypatch, handler)
    ok, message = notifier.test_connection()
    assert not ok and len(calls) == 1
    assert notifier.token not in message
    assert '403' in message
    assert 'password' not in notifier._safe_error(Exception('socks5://user:password@proxy.test:1080'))


def test_get_updates_uses_same_proxy_transport_and_long_poll_timeout(notifier, monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'ok': True, 'result': []})
    install_transport(monkeypatch, handler)
    assert notifier.get_updates(timeout=30) == []
    assert requests[0].extensions['timeout']['read'] == 45
    assert requests[0].extensions['timeout']['connect'] == 10


def test_long_poll_error_redacts_token_before_upper_layer_logging(notifier, monkeypatch):
    install_transport(monkeypatch, lambda request: httpx.Response(403, json={'ok': False}))
    with pytest.raises(RuntimeError) as caught:
        notifier.get_updates()
    assert notifier.token not in str(caught.value)
