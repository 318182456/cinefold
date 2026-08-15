"""封面人像面的批量判断。

portrait_side 在下载封面时顺手算一次，但判断算法改了、或存量记录还没判过
时，需要整库重跑一遍。图库动辄几万张，同步执行必然把 HTTP 请求拖到超时，
所以走后台线程 + 进度轮询，跟 watchdir 的批量同步一个路子。
"""
from __future__ import annotations

import threading

from loguru import logger
from sqlalchemy import select

from app.database.models import Code
from app.database.session import session_scope
from app.utils import imagecache, imgcrop

# 进度状态。只有一个批量任务，不需要按 id 分槽
_lock = threading.Lock()
_state: dict = {"running": False, "total": 0, "done": 0, "message": "", "tally": {}}


def get_progress() -> dict:
    with _lock:
        return dict(_state)


def is_running() -> bool:
    with _lock:
        return bool(_state["running"])


def _reset(total: int) -> None:
    with _lock:
        _state.update(
            running=True, total=total, done=0, message="正在判断", tally={}
        )


def _advance(side: str) -> None:
    with _lock:
        _state["done"] += 1
        _state["tally"][side] = _state["tally"].get(side, 0) + 1


def _finish(message: str) -> None:
    with _lock:
        _state.update(running=False, message=message)


def redetect_all(only_missing: bool = False) -> int:
    """重判所有已缓存封面的人像面，返回更新条数。

    only_missing 为真时只补没判过的；默认全量重算 —— 判断算法调整后
    存量结果都得刷新，这也是这个功能存在的理由。
    """
    with session_scope() as session:
        query = select(Code.code, Code.local_banner).where(
            (Code.local_banner.isnot(None)) & (Code.local_banner != "")
        )
        if only_missing:
            query = query.where(
                (Code.portrait_side.is_(None)) | (Code.portrait_side == "")
            )
        rows = session.execute(query).all()

    _reset(len(rows))
    if not rows:
        _finish("没有需要判断的封面")
        return 0

    updates: list[tuple[str, str]] = []
    missing = 0
    for code, local_banner in rows:
        relative = (local_banner or "").split(",")[0]
        path = imagecache.resolve_relative(relative)
        if path is None:
            missing += 1
            continue

        side = imgcrop.detect_from_file(path, code)
        updates.append((code, side))
        _advance(side)

        # 分批落库，几万条攒到最后一次性写会把内存和事务撑得很大
        if len(updates) >= 500:
            _flush(updates)
            updates = []

    if updates:
        _flush(updates)

    with _lock:
        tally = dict(_state["tally"])
    total_done = sum(tally.values())
    summary = "，".join(f"{k} {v}" for k, v in sorted(tally.items()))
    message = f"已判断 {total_done} 张（{summary}）"
    if missing:
        message += f"，{missing} 张图片文件不存在"
    _finish(message)
    logger.info(f"封面人像面重判完成：{message}")
    return total_done


def _flush(updates: list[tuple[str, str]]) -> None:
    with session_scope() as session:
        for code, side in updates:
            row = session.get(Code, code)
            if row is not None:
                row.portrait_side = side


def start_redetect(only_missing: bool = False) -> bool:
    """后台启动重判。已经在跑就返回 False，不重复启动。"""
    if is_running():
        return False

    def run() -> None:
        try:
            redetect_all(only_missing=only_missing)
        except Exception as exc:
            logger.exception(f"封面人像面重判异常: {exc}")
            _finish(f"判断异常: {exc}")

    threading.Thread(target=run, daemon=True).start()
    return True
