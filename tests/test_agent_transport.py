"""助手实际 HTTP 请求的出口、握手失败恢复与工具去重，不连接真实模型。"""
import json
import ssl
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from app.modules.agent import agent as module


@pytest.fixture
def transport(monkeypatch):
    settings = SimpleNamespace(agent_enabled=True, agent_url="https://gateway.test/v1",
                               agent_model="model", agent_api_key="secret", proxy="http://fallback.test")
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    leases, feedback, clients, requests = [], [], [], []

    @contextmanager
    def lease(fallback=None, **kwargs):
        leases.append((fallback, kwargs))
        yield f"http://node-{len(leases)}.test"

    monkeypatch.setattr(module, "proxy_lease", lease)
    monkeypatch.setattr(module, "get_manager", lambda: SimpleNamespace(
        note_result=lambda *args: feedback.append(args)))
    original_client = httpx.Client
    handler = [lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})]

    def client(**kwargs):
        clients.append(kwargs)
        assert kwargs["trust_env"] is False and "verify" not in kwargs
        assert kwargs["timeout"].connect == 10

        def respond(request):
            requests.append(request)
            return handler[0](request)

        # 不把测试代理传进真实客户端，只记录调用方选择的出口。
        return original_client(transport=httpx.MockTransport(respond), trust_env=False)

    monkeypatch.setattr(module.httpx, "Client", client)
    return SimpleNamespace(agent=module.ChatAgent(), leases=leases, feedback=feedback,
                           clients=clients, requests=requests, handler=handler)


def test_tls_connect_failure_rotates_and_preserves_authenticated_body(transport):
    def reply(request):
        if len(transport.requests) == 1:
            raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING]", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "recovered"}}]})

    transport.handler[0] = reply
    assert transport.agent.chat("hello")["answer"] == "recovered"
    assert [c["proxy"] for c in transport.clients] == ["http://node-1.test", "http://node-2.test"]
    assert [f[-1] for f in transport.feedback] == [False, True]
    first, second = transport.requests
    assert first.content == second.content
    assert first.headers["Authorization"] == second.headers["Authorization"] == "Bearer secret"
    assert all(l[0] == "http://fallback.test" and l[1] == {
        "source_key": "agent", "url": "https://gateway.test/v1/chat/completions"} for l in transport.leases)


def test_connect_retries_are_bounded_and_error_is_actionable(transport):
    def fail(request):
        raise httpx.ConnectError("private network detail", request=request)
    transport.handler[0] = fail
    answer = transport.agent.chat("hello")["answer"]
    assert len(transport.requests) == 2
    assert "代理节点" in answer and "private network detail" not in answer


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError])
def test_failures_after_connect_do_not_resubmit_model_request(transport, error):
    def fail(request):
        raise error("lost response", request=request)
    transport.handler[0] = fail
    assert "连接失败" in transport.agent.chat("hello")["answer"]
    assert len(transport.requests) == 1 and transport.feedback == []


def test_certificate_failure_is_not_retried_or_downgraded(transport):
    def fail(request):
        try:
            raise ssl.SSLCertVerificationError("untrusted issuer")
        except ssl.SSLCertVerificationError as exc:
            raise httpx.ConnectError("connection failed", request=request) from exc
    transport.handler[0] = fail
    assert "证书" in transport.agent.chat("hello")["answer"]
    assert len(transport.requests) == 1 and transport.feedback == []


def test_authentication_failure_is_not_a_bad_node(transport):
    transport.handler[0] = lambda request: httpx.Response(401, json={"error": "invalid key"})
    assert "401" in transport.agent.chat("hello")["answer"]
    assert len(transport.requests) == 1 and transport.feedback[0][-1] is True


@pytest.mark.parametrize("tool", ["overview", "propose_action"])
def test_retry_after_tool_call_does_not_execute_the_tool_again(transport, monkeypatch, tool):
    executed = []
    payload = {"proposal": {"id": "only-one"}} if tool == "propose_action" else {"count": 1}
    monkeypatch.setattr(module, "call_tool", lambda *args: (executed.append(args) or json.dumps(payload)))
    def reply(request):
        number = len(transport.requests)
        if number == 1:
            return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
                {"id": "one", "function": {"name": tool, "arguments": "{}"}}
            ]}}]})
        if number == 2:
            raise httpx.ConnectError("TLS EOF", request=request)
        assert json.loads(request.content)["messages"][-1]["role"] == "tool"
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})
    transport.handler[0] = reply
    result = transport.agent.chat("status")
    assert result["answer"] == "done" and result["tools_used"] == [tool]
    assert result["proposals"] == ([payload["proposal"]] if tool == "propose_action" else [])
    assert len(executed) == 1 and transport.requests[1].content == transport.requests[2].content


def test_cancel_during_preflight_prevents_model_request(transport, monkeypatch):
    stop = threading.Event()
    @contextmanager
    def lease(*args, **kwargs):
        stop.set()
        yield "http://node.test"
    monkeypatch.setattr(module, "proxy_lease", lease)
    assert "已停止" in transport.agent.chat("hello", stop=stop)["answer"]
    assert transport.requests == [] and transport.clients == []
