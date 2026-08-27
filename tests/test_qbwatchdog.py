"""qBittorrent 连接自愈的判定逻辑。

重点不是「能重启」，而是「不该重启的时候绝不重启」—— 重启会打断正在
下载的任务，误触发的代价比漏触发大。
"""
from __future__ import annotations

import qbittorrentapi
import pytest
import requests

from app.services import qbwatchdog


@pytest.fixture(autouse=True)
def clean_state():
    qbwatchdog.reset_state()
    yield
    qbwatchdog.reset_state()


@pytest.fixture
def restarts(monkeypatch):
    """拦下真正的 Docker 调用，记录被触发了几次。

    重启走后台线程，测试里换成同步执行，省掉等线程的不确定性。
    """
    calls: list[str] = []

    def fake_restart():
        calls.append("restart")
        return True, "已重启容器 qbittorrent"

    monkeypatch.setattr("app.services.dockerctl.restart_qbittorrent", fake_restart)

    class SyncThread:
        def __init__(self, target=None, args=(), **kwargs):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(qbwatchdog.threading, "Thread", SyncThread)
    return calls


def _enable(monkeypatch, **overrides):
    """打开自愈开关。settings 是 dataclass，直接改字段即可。"""
    from app.core.config import get_settings

    settings = get_settings()
    values = {
        "qb_autoheal_enabled": True,
        "qb_autoheal_failures": 3,
        "qb_autoheal_cooldown": 15,
        "qb_autoheal_notify": False,
        "docker_container_qbittorrent": "qbittorrent",
        **overrides,
    }
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value, raising=False)
    return settings


TIMEOUT = qbittorrentapi.APIConnectionError(
    "Failed to connect to qBittorrent. Unknown Error: ReadTimeout("
    'ReadTimeoutError("HTTPConnectionPool(host=\'192.168.3.11\', port=8086): '
    'Read timed out. (read timeout=30)"))'
)


# ---------------------------------------------------------------- 异常分类
def test_识别真实日志里的超时():
    assert qbwatchdog.is_connection_error(TIMEOUT)


@pytest.mark.parametrize("exc", [
    requests.exceptions.ReadTimeout("Read timed out."),
    requests.exceptions.ConnectionError("Connection refused"),
])
def test_识别各类连接异常(exc):
    assert qbwatchdog.is_connection_error(exc)


@pytest.mark.parametrize("exc", [
    qbittorrentapi.Forbidden403Error("forbidden"),
    qbittorrentapi.NotFound404Error("torrent not found"),
    qbittorrentapi.LoginFailed("bad credentials"),
    ValueError("bad hash"),
])
def test_业务错误不算连接故障(exc):
    """qb 能返回 403/404 说明它活着，重启解决不了配置问题。"""
    assert not qbwatchdog.is_connection_error(exc)


def test_识别被包装在内层的连接异常():
    """qbittorrent-api 会把底层异常包起来，只看最外层类型名会漏。"""
    try:
        raise requests.exceptions.ReadTimeout("Read timed out.")
    except requests.exceptions.ReadTimeout as inner:
        outer = RuntimeError("operation failed")
        outer.__cause__ = inner
        assert qbwatchdog.is_connection_error(outer)


# ---------------------------------------------------------------- 计数与触发
def test_未达阈值不重启(monkeypatch, restarts):
    _enable(monkeypatch)
    for _ in range(2):
        qbwatchdog.report_failure(TIMEOUT, "查询任务状态")
    assert restarts == []
    assert qbwatchdog.get_state()["failures"] == 2


def test_达到阈值触发重启(monkeypatch, restarts):
    _enable(monkeypatch)
    for _ in range(3):
        qbwatchdog.report_failure(TIMEOUT, "查询任务状态")
    assert restarts == ["restart"]
    # 重启后清零，重新攒够才允许下一次
    assert qbwatchdog.get_state()["failures"] == 0


def test_一次成功清零计数(monkeypatch, restarts):
    """计的是「连续」失败。中途恢复过就不该累计到重启。"""
    _enable(monkeypatch)
    qbwatchdog.report_failure(TIMEOUT)
    qbwatchdog.report_failure(TIMEOUT)
    qbwatchdog.report_success()
    qbwatchdog.report_failure(TIMEOUT)
    qbwatchdog.report_failure(TIMEOUT)
    assert restarts == []
    assert qbwatchdog.get_state()["failures"] == 2


def test_业务错误不计入(monkeypatch, restarts):
    _enable(monkeypatch)
    for _ in range(10):
        qbwatchdog.report_failure(qbittorrentapi.NotFound404Error("no such torrent"))
    assert restarts == []
    assert qbwatchdog.get_state()["failures"] == 0


def test_开关关闭时不动手(monkeypatch, restarts):
    _enable(monkeypatch, qb_autoheal_enabled=False)
    for _ in range(5):
        qbwatchdog.report_failure(TIMEOUT)
    assert restarts == []


def test_冷却期内不重复重启(monkeypatch, restarts):
    """qb 重启后要几十秒才响应 WebAPI，这期间的失败不能再触发重启。"""
    _enable(monkeypatch)
    for _ in range(3):
        qbwatchdog.report_failure(TIMEOUT)
    assert restarts == ["restart"]

    # 紧接着又攒够一轮，但还在冷却期内
    for _ in range(3):
        qbwatchdog.report_failure(TIMEOUT)
    assert restarts == ["restart"]


def test_冷却期过后允许再次重启(monkeypatch, restarts):
    _enable(monkeypatch)
    for _ in range(3):
        qbwatchdog.report_failure(TIMEOUT)
    assert restarts == ["restart"]

    # 把时钟推过冷却期
    base = qbwatchdog.time.monotonic()
    monkeypatch.setattr(qbwatchdog.time, "monotonic", lambda: base + 16 * 60)
    for _ in range(3):
        qbwatchdog.report_failure(TIMEOUT)
    assert restarts == ["restart", "restart"]


def test_重启期间的失败不叠加(monkeypatch):
    """重启中 qb 必然连不上，这批失败不该再触发第二次重启。"""
    _enable(monkeypatch)
    calls: list[str] = []

    def reentrant_restart():
        calls.append("restart")
        # 模拟重启过程中其它线程仍在打 qb，请求全部超时
        for _ in range(5):
            qbwatchdog.report_failure(TIMEOUT, "重启期间")
        return True, "ok"

    monkeypatch.setattr("app.services.dockerctl.restart_qbittorrent", reentrant_restart)

    class SyncThread:
        def __init__(self, target=None, args=(), **kwargs):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(qbwatchdog.threading, "Thread", SyncThread)

    for _ in range(3):
        qbwatchdog.report_failure(TIMEOUT)
    assert calls == ["restart"]
    assert qbwatchdog.get_state()["failures"] == 0


def test_阈值配成零按一次处理(monkeypatch, restarts):
    """配置写 0 或负数不该变成「永不重启」或「除零」。"""
    _enable(monkeypatch, qb_autoheal_failures=0)
    qbwatchdog.report_failure(TIMEOUT)
    assert restarts == ["restart"]


def test_重启失败也进入冷却(monkeypatch):
    """否则每次失败都会再打一遍 Docker API。"""
    _enable(monkeypatch)
    calls: list[str] = []

    def failing_restart():
        calls.append("restart")
        return False, "连不上 Docker"

    monkeypatch.setattr("app.services.dockerctl.restart_qbittorrent", failing_restart)

    class SyncThread:
        def __init__(self, target=None, args=(), **kwargs):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(qbwatchdog.threading, "Thread", SyncThread)

    for _ in range(3):
        qbwatchdog.report_failure(TIMEOUT)
    assert calls == ["restart"]

    for _ in range(3):
        qbwatchdog.report_failure(TIMEOUT)
    assert calls == ["restart"]


# ---------------------------------------------------------------- 客户端接线
def test_连接故障后丢掉客户端(monkeypatch):
    """qb 卡死或重启过后旧 session 已失效，留着它就永远不会重新登录。"""
    from app.modules.downloadclient.qbittorrent import QBitTorrentClient

    client = QBitTorrentClient(url="http://127.0.0.1:8080")
    client.client = object()
    client._on_error(TIMEOUT, "查询任务状态")
    assert client.client is None


def test_业务错误保留客户端(monkeypatch):
    from app.modules.downloadclient.qbittorrent import QBitTorrentClient

    client = QBitTorrentClient(url="http://127.0.0.1:8080")
    sentinel = object()
    client.client = sentinel
    client._on_error(qbittorrentapi.NotFound404Error("no such torrent"), "删除种子")
    assert client.client is sentinel


def test_导出种子超时不计入健康判断(monkeypatch, restarts):
    """导出超时说明「这个种子难导」，不是「qb 坏了」。

    实测现场：导出时 qb CPU 96%、磁盘 0B —— 进程活得好好的，只是被这一个
    请求占住。计进去会攒够阈值重启一个健康的 qb，把正在下载的任务全打断。
    """
    _enable(monkeypatch)
    for _ in range(5):
        qbwatchdog.report_failure(TIMEOUT, "导出种子")
    assert restarts == []
    assert qbwatchdog.get_state()["failures"] == 0


def test_导出超时不影响其他操作的计数(monkeypatch, restarts):
    """导出被排除，但别的操作照常计数 —— 真卡死了还是要救。"""
    _enable(monkeypatch)
    qbwatchdog.report_failure(TIMEOUT, "导出种子")
    assert qbwatchdog.get_state()["failures"] == 0

    for _ in range(3):
        qbwatchdog.report_failure(TIMEOUT, "查询任务状态")
    assert restarts == ["restart"]


def test_真正的卡死仍能触发重启(monkeypatch, restarts):
    """轻操作上的超时才是卡死的证据，这条路必须留着。"""
    _enable(monkeypatch)
    for ctx in ("查询任务状态", "读取种子详情", "推送种子"):
        qbwatchdog.report_failure(TIMEOUT, ctx)
    assert restarts == ["restart"]
