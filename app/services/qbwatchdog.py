"""qBittorrent 连接自愈：连不上时重启它的容器。

qb 偶发卡死 —— WebAPI 不再响应但进程还在，表现为请求全部 read timeout。
这种状态不会自己恢复，转移做种、状态同步全都停摆，只能重启容器。

判定逻辑刻意保守，因为重启是有代价的（正在下载的任务会断开重连，
校验中的任务要重新校验）：

- 只有「连接类失败」才计数。业务错误（404、种子不存在、参数错）说明
  qb 活得很好，不该计入
- 连续 N 次才动手。网络抖动、qb 忙时偶发超时很常见，单次失败就重启
  会变成反复重启
- 一次成功立刻清零。计的是「连续」失败，不是累计
- 两次重启之间有冷却期。qb 重启后要几十秒才能响应 WebAPI，这期间的
  请求照样超时，没有冷却就会连着重启好几轮
- 重启后计数清零，让它重新攒够 N 次才允许下一次重启

状态只存内存：进程重启后本该从零开始判断，落库反而会让重启后的第一次
失败就直接触发重启。
"""
from __future__ import annotations

import threading
import time

from loguru import logger

from app.core.config import get_settings

# 判定为连接类故障的异常类型名。按名字匹配而不是 import 类型 ——
# qbittorrent-api 的异常层级随版本变动，requests / urllib3 的超时异常
# 又会被它包装成不同外壳，名字比继承关系稳定
_CONNECTION_ERROR_NAMES = {
    "APIConnectionError",      # qbittorrent-api 对连接失败的统一包装
    "ConnectionError",
    "ConnectTimeout",
    "ReadTimeout",
    "Timeout",
    "ReadTimeoutError",
    "ConnectTimeoutError",
    "MaxRetryError",
    "NewConnectionError",
    "ProtocolError",
    "RemoteDisconnected",
    "ChunkedEncodingError",
    "SSLError",
}

# 异常消息里出现这些片段也算连接故障。qbittorrent-api 常把底层异常
# 拼成字符串塞进自己的异常里，类型名就看不出来了
_CONNECTION_ERROR_HINTS = (
    "timed out",
    "timeout",
    "failed to connect",
    "connection refused",
    "connection reset",
    "connection aborted",
    "max retries exceeded",
    "name or service not known",
    "temporary failure in name resolution",
    "no route to host",
    "network is unreachable",
    "远程主机强迫关闭",
)

_lock = threading.Lock()
# 连续连接失败次数
_failures = 0
# 上次重启的时间戳（time.monotonic），0 表示本进程还没重启过
_last_restart = 0.0
# 正在重启中。重启期间的失败不再累加，也不允许并发触发第二次重启
_restarting = False


def is_connection_error(exc: BaseException) -> bool:
    """这个异常是否说明「qb 连不上」，而不是某次操作本身出错。

    逐层看 __cause__ / __context__：qbittorrent-api 包装底层异常时，
    最外层的类型名往往已经看不出连接问题了。
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _CONNECTION_ERROR_NAMES:
            return True
        text = str(current).lower()
        if any(hint in text for hint in _CONNECTION_ERROR_HINTS):
            return True
        current = current.__cause__ or current.__context__
    return False


def report_success() -> None:
    """qb 有正常响应。清零连续失败计数。"""
    global _failures
    if _failures:
        with _lock:
            if _failures:
                logger.debug(f"[qb 自愈] qBittorrent 已恢复响应，清零失败计数（原 {_failures}）")
                _failures = 0


def report_failure(exc: BaseException | None = None, context: str = "") -> None:
    """qb 连接失败。攒够阈值就在后台线程里重启它的容器。

    异常传进来时会先判断是不是连接类故障，业务错误直接忽略。调用方已经
    确认是连接问题时可以不传。

    重启放后台线程：调用方多半在处理种子的循环里，restart 要等容器停干净，
    最长几十秒，不能让它把整轮扫描卡住。
    """
    global _failures, _restarting

    if exc is not None and not is_connection_error(exc):
        return

    settings = get_settings()
    if not settings.qb_autoheal_enabled:
        return

    threshold = max(1, int(settings.qb_autoheal_failures or 1))

    with _lock:
        if _restarting:
            return
        _failures += 1
        current = _failures
        if current < threshold:
            logger.warning(
                f"[qb 自愈] qBittorrent 连接失败 {current}/{threshold}"
                f"{f'（{context}）' if context else ''}"
            )
            return

        cooldown = max(0, int(settings.qb_autoheal_cooldown or 0)) * 60
        elapsed = time.monotonic() - _last_restart
        if _last_restart and elapsed < cooldown:
            # 冷却期内计数会停在阈值之上，之后每次失败都会走到这里。
            # 只在刚跨过阈值那次说明情况，避免 qb 长时间不可用时刷满日志
            if current == threshold:
                logger.warning(
                    f"[qb 自愈] 连接失败已达 {current} 次，但距上次重启仅 "
                    f"{int(elapsed // 60)} 分钟，未到冷却期 {cooldown // 60} 分钟，暂不重启"
                )
            return

        _restarting = True

    threading.Thread(
        target=_do_restart, args=(current, context), name="qb-autoheal", daemon=True
    ).start()


def _do_restart(failures: int, context: str) -> None:
    """真正执行重启，并把结果通知出去。

    无论成败都要清零计数并记下重启时间：成功了自然要重新攒；失败了也得靠
    冷却期挡住下一次尝试，否则每次失败都会再打一遍 Docker API。
    """
    global _failures, _last_restart, _restarting

    from app.services.dockerctl import restart_qbittorrent

    reason = f"连续 {failures} 次连接失败" + (f"（{context}）" if context else "")
    logger.warning(f"[qb 自愈] {reason}，尝试重启 qBittorrent 容器")

    ok, message = False, "未执行"
    try:
        ok, message = restart_qbittorrent()
    except Exception as exc:
        message = str(exc)
        logger.exception(f"[qb 自愈] 重启 qBittorrent 容器时异常: {exc}")
    finally:
        with _lock:
            _failures = 0
            _last_restart = time.monotonic()
            _restarting = False

    _notify(ok, reason, message)


def _notify(ok: bool, reason: str, message: str) -> None:
    """把重启结果推给通知渠道。通知失败不影响自愈本身。"""
    if not get_settings().qb_autoheal_notify:
        return
    try:
        from app.modules.notify import broadcast_text

        head = "✅ qBittorrent 已自动重启" if ok else "⚠️ qBittorrent 自动重启失败"
        broadcast_text(f"{head}\n原因：{reason}\n结果：{message}")
    except Exception as exc:
        logger.warning(f"[qb 自愈] 发送通知失败: {exc}")


def get_state() -> dict:
    """当前自愈状态，供接口展示与测试断言。"""
    settings = get_settings()
    with _lock:
        last = _last_restart
        return {
            "enabled": settings.qb_autoheal_enabled,
            "failures": _failures,
            "threshold": max(1, int(settings.qb_autoheal_failures or 1)),
            "cooldown_minutes": max(0, int(settings.qb_autoheal_cooldown or 0)),
            "restarting": _restarting,
            # 距上次重启过了多少秒。本进程未重启过时为 None
            "since_last_restart": None if not last else int(time.monotonic() - last),
        }


def reset_state() -> None:
    """清空计数与冷却。改完配置或手动处理完故障后用，测试也用它隔离。"""
    global _failures, _last_restart, _restarting
    with _lock:
        _failures = 0
        _last_restart = 0.0
        _restarting = False
