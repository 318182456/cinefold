"""Docker 重启容器的 HTTP 交互。

用一个假的 Docker daemon（本地 HTTP server）跑，重点验证 URL 拼装与各类
状态码的处理 —— 尤其是「容器名写错」与「daemon 连不上」必须给出不同的
提示，两者的处置方式完全不同。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.services import dockerctl


class _FakeDocker(BaseHTTPRequestHandler):
    # 由测试逐项覆盖
    containers: dict = {}
    restart_status: int = 204
    requests_seen: list = []

    def log_message(self, *args):
        pass

    def _send(self, code: int, body: dict | None = None):
        payload = json.dumps(body or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        type(self).requests_seen.append(("GET", self.path))
        if self.path.endswith("/version"):
            return self._send(200, {"Version": "27.1.1"})

        name = self.path.rsplit("/", 2)[-2] if self.path.endswith("/json") else ""
        state = type(self).containers.get(name)
        if state is None:
            return self._send(404, {"message": "No such container"})
        return self._send(200, {"State": {"Status": state}})

    def do_POST(self):
        type(self).requests_seen.append(("POST", self.path))
        name = self.path.rsplit("/", 2)[-2]
        if name not in type(self).containers:
            return self._send(404, {"message": "No such container"})
        code = type(self).restart_status
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def fake_docker():
    """起一个假 daemon，返回它的 tcp:// 地址。"""
    _FakeDocker.containers = {"qbittorrent": "running"}
    _FakeDocker.restart_status = 204
    _FakeDocker.requests_seen = []

    server = HTTPServer(("127.0.0.1", 0), _FakeDocker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"tcp://127.0.0.1:{server.server_port}", _FakeDocker
    finally:
        server.shutdown()
        server.server_close()


def _configure(monkeypatch, host: str, container: str = "qbittorrent"):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "docker_host", host, raising=False)
    monkeypatch.setattr(
        settings, "docker_container_qbittorrent", container, raising=False
    )


# ---------------------------------------------------------------- 地址解析
@pytest.mark.parametrize("host,expect_base", [
    ("tcp://192.168.3.11:2375", "http://192.168.3.11:2375"),
    ("http://192.168.3.11:2375", "http://192.168.3.11:2375"),
    # 没写协议时按 http 补全
    ("192.168.3.11:2375", "http://192.168.3.11:2375"),
])
def test_tcp地址归一化(host, expect_base):
    client, base = dockerctl._client_for(host)
    with client:
        assert base == expect_base


def test_unix_socket走uds传输():
    """unix socket 的主机名不参与连接，只要 transport 挂上 uds 即可。"""
    client, base = dockerctl._client_for("unix:///var/run/docker.sock")
    with client:
        assert base == "http://localhost"


# ---------------------------------------------------------------- 重启
def test_重启成功(monkeypatch, fake_docker):
    host, handler = fake_docker
    _configure(monkeypatch, host)

    ok, message = dockerctl.restart_container()
    assert ok
    assert "qbittorrent" in message
    # 先 inspect 再 restart：直接 restart 时分不清是名字错还是 daemon 不通
    methods = [m for m, _ in handler.requests_seen]
    assert methods == ["GET", "POST"]


def test_已在重启中也算成功(monkeypatch, fake_docker):
    host, handler = fake_docker
    handler.restart_status = 304
    _configure(monkeypatch, host)

    ok, _ = dockerctl.restart_container()
    assert ok


def test_容器名不存在给出明确提示(monkeypatch, fake_docker):
    host, _ = fake_docker
    _configure(monkeypatch, host, container="qb-typo")

    ok, message = dockerctl.restart_container()
    assert not ok
    assert "没有名为 qb-typo 的容器" in message


def test_daemon连不上时区别于容器不存在(monkeypatch):
    # 一个必定没人监听的端口
    _configure(monkeypatch, "tcp://127.0.0.1:1")

    ok, message = dockerctl.restart_container()
    assert not ok
    assert "连不上 Docker" in message


def test_未配容器名不动手(monkeypatch, fake_docker):
    host, handler = fake_docker
    _configure(monkeypatch, host, container="")

    ok, message = dockerctl.restart_container()
    assert not ok
    assert "未配置" in message
    # 配置不全时一个请求都不该发出去
    assert handler.requests_seen == []


def test_未配docker地址不动手(monkeypatch, fake_docker):
    _configure(monkeypatch, "")
    ok, message = dockerctl.restart_container()
    assert not ok
    assert "未配置 Docker 地址" in message


# ---------------------------------------------------------------- 连接测试
def test_测试连接报告版本与容器状态(monkeypatch, fake_docker):
    host, _ = fake_docker
    _configure(monkeypatch, host)

    ok, message = dockerctl.test_connection()
    assert ok
    assert "27.1.1" in message
    assert "running" in message


def test_测试连接容器不存在时算失败(monkeypatch, fake_docker):
    host, _ = fake_docker
    _configure(monkeypatch, host, container="qb-typo")

    ok, message = dockerctl.test_connection()
    assert not ok
    assert "qb-typo" in message


def test_测试连接不真的重启(monkeypatch, fake_docker):
    host, handler = fake_docker
    _configure(monkeypatch, host)

    dockerctl.test_connection()
    assert all(method == "GET" for method, _ in handler.requests_seen)


def test_unix_socket不通时提示挂载(monkeypatch):
    """最常见的配置失误就是忘了把 docker.sock 挂进容器。"""
    _configure(monkeypatch, "unix:///nonexistent/docker.sock")

    ok, message = dockerctl.test_connection()
    assert not ok
    assert "docker.sock" in message
