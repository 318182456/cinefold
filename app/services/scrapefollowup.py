"""Bounded background work after a video and its metadata are committed."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from html import escape
import threading
import time

from loguru import logger

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="scrape-followup")
_CAPACITY = threading.BoundedSemaphore(4)
_LOCK = threading.Lock()
_PENDING: set[str] = set()


def schedule(code: str, title: str, count: int, *, subtitles: bool = False,
             review: bool = False, notify: bool = False) -> str:
    """Return queued, pending, full, disabled or error without blocking import."""
    if not (subtitles or review or notify):
        return "disabled"
    identity = code.upper()
    with _LOCK:
        if identity in _PENDING:
            return "pending"
        if not _CAPACITY.acquire(blocking=False):
            logger.warning(f"[{code}] 后台补全队列已满，跳过本次字幕、看点和通知；可稍后补全")
            return "full"
        _PENDING.add(identity)

    def finished(future):
        with _LOCK:
            _PENDING.discard(identity)
        _CAPACITY.release()
        if not future.cancelled():
            try:
                future.result()
            except Exception:
                logger.exception(f"[{code}] 入库后后台补全任务异常")

    try:
        future = _POOL.submit(_run, code, title, count, subtitles, review, notify)
    except Exception:
        with _LOCK:
            _PENDING.discard(identity)
        _CAPACITY.release()
        logger.exception(f"[{code}] 无法启动入库后后台补全任务")
        return "error"
    future.add_done_callback(finished)
    logger.info(f"[{code}] 已提交后台补全，影片入库任务不再等待字幕、看点或通知")
    return "queued"


def _run(code, title, count, subtitles, review, notify):
    from app.services import medialink
    actions = []
    if notify:
        actions.append(("通知", lambda: _notify(code, title, count)))
    if subtitles:
        actions.append(("字幕", lambda: medialink._fetch_subtitle_quietly(code)))
    if review:
        actions.append(("AI 看点", lambda: medialink._generate_review_quietly(code)))
    for name, action in actions:
        started = time.perf_counter()
        logger.info(f"[{code}] 后台{name}开始")
        try:
            action()
        except Exception:
            logger.exception(f"[{code}] 后台{name}失败，可稍后补全")
        finally:
            logger.info(f"[{code}] 后台{name}结束，耗时 {time.perf_counter() - started:.2f}s")


def _notify(code, title, count):
    from app.modules.notify import get_notifiers
    text = f"刮削入库完成：{escape(code)}\n{escape(title)}\n{count} 个影片文件"
    for notifier in get_notifiers():
        try:
            if not notifier.send_text_message(text):
                logger.warning(f"[{code}] 后台通知发送失败: {type(notifier).__name__}")
        except Exception:
            logger.exception(f"[{code}] 后台通知异常: {type(notifier).__name__}")
