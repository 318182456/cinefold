"""Telegram 长轮询。

没有公网 HTTPS 时用它代替 webhook：起一个后台线程反复调 getUpdates，
取到消息后交给和 webhook 相同的处理入口。
"""
from __future__ import annotations

import threading

from loguru import logger

from app.core.config import get_settings

# 单次长轮询挂起的秒数。Telegram 端最长 50s，取 30s 兼顾响应与重连频率
POLL_TIMEOUT = 30
# 连续出错后的退避上限，避免网络断开时刷屏
MAX_BACKOFF = 60

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
# 每次启动递增。旧线程可能还挂在长轮询上，醒来发现代号变了就自行退出，
# 这样重启不必阻塞等待，也不会出现两个线程同时 getUpdates 抢消息
_generation = 0


def _loop(generation: int) -> None:
    from app.api.endpoints.message import handle_telegram_update
    from app.modules.notify.telegram import TelegramNotifier

    offset = 0
    backoff = 1

    def outdated() -> bool:
        return _stop.is_set() or generation != _generation

    # webhook 与 getUpdates 互斥，Telegram 会直接拒绝后者
    notifier = TelegramNotifier()
    if (notifier.get_webhook_info() or {}).get("url"):
        ok, message = notifier.delete_webhook()
        logger.info(f"[TG] 切换到 polling，删除 webhook: {message}")
        if not ok:
            logger.warning("[TG] webhook 未能删除，getUpdates 可能持续失败")

    logger.info("[TG] 长轮询已启动")
    while not outdated():
        try:
            # 每轮重建，配置改了 token/代理后无需重启线程
            updates = TelegramNotifier().get_updates(offset, POLL_TIMEOUT)
            backoff = 1
        except Exception as exc:
            if outdated():
                break
            logger.warning(f"[TG] 拉取消息失败，{backoff}s 后重试: {exc}")
            _stop.wait(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue

        # 长轮询期间可能已被停止或重启，这批消息交给新线程重新取
        if outdated():
            break

        for update in updates:
            # 先推进 offset 再处理：处理失败也不该让同一条消息卡住整个队列
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            try:
                handle_telegram_update(update)
            except Exception as exc:
                logger.exception(f"[TG] 处理消息失败: {exc}")

    logger.info("[TG] 长轮询已停止")


def start_polling() -> bool:
    """按配置启动长轮询。已在运行或不该启动时返回 False。"""
    global _thread, _generation

    settings = get_settings()
    if settings.telegram_receive_mode != "polling":
        return False
    if not settings.telegram_bot_token:
        logger.warning("[TG] 接收方式为 polling 但未配置 Bot Token，跳过")
        return False

    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop.clear()
        _generation += 1
        _thread = threading.Thread(
            target=_loop, args=(_generation,), name="tg-polling", daemon=True
        )
        _thread.start()
    return True


def stop_polling() -> None:
    """通知轮询线程退出，不等待它真正结束。

    线程最长还会在 getUpdates 上挂 POLL_TIMEOUT 秒，但代号已失效，
    醒来就会退出，不影响新线程接手。
    """
    global _thread, _generation

    with _lock:
        if _thread is None:
            return
        _stop.set()
        _generation += 1
        _thread = None


def restart_polling() -> None:
    """配置变更后按新设置重挂。切到 webhook 时相当于只停不起。"""
    stop_polling()
    start_polling()


def is_polling() -> bool:
    return _thread is not None and _thread.is_alive()
