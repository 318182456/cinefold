"""监控目录同步的进度状态。

sync_rule 全程同步执行，中途没有任何反馈 —— 而它慢起来是真的慢：
每个新文件都要走 _wait_stable()（最多 sleep 2s × 3 轮），一批新文件下来
几分钟没动静，页面上完全看不出它是在干活还是卡死了。

所以在这里存一份「当前跑到哪了」，只放内存：

  · 进度是瞬时状态，重启后本来就该清空，落库没有意义
  · 每处理一个文件写一次库，对 SQLite 是不必要的写放大

多 worker 部署时每个进程各看到自己的那份。cinefold 默认单进程，
真要多开时进度显示不全，但同步本身不受影响 —— 这是可接受的取舍。
"""
from __future__ import annotations

import threading
from datetime import datetime

_lock = threading.Lock()

# rule_id → 进度快照
_state: dict[int, dict] = {}


def start(rule_id: int, name: str, total: int) -> None:
    """开始一轮同步。total 是这轮要检查的文件数。"""
    with _lock:
        _state[rule_id] = {
            "rule_id": rule_id,
            "name": name,
            "running": True,
            "phase": "scanning",
            "total": total,
            "done": 0,
            "current": "",
            "linked": 0,
            "unlinked": 0,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": "",
            "message": "",
        }


def update(
    rule_id: int, *, phase: str = "", current: str = "",
    total: int | None = None, done: int | None = None,
    linked: int | None = None, unlinked: int | None = None,
    message: str = "",
) -> None:
    """更新进度。只改传进来的字段，其余保持原值。

    current 传空字符串不会清空原值（空值一律视为「不改」）。要清空得用
    finish()，那是唯一需要清空的场景。
    """
    with _lock:
        item = _state.get(rule_id)
        if item is None:
            return
        if phase:
            item["phase"] = phase
        if current:
            item["current"] = current
        if total is not None:
            item["total"] = total
        if done is not None:
            item["done"] = done
        if linked is not None:
            item["linked"] = linked
        if unlinked is not None:
            item["unlinked"] = unlinked
        if message:
            item["message"] = message


def finish(rule_id: int, message: str = "") -> None:
    """标记完成。记录保留着，页面还能看到最后一轮的结果。"""
    with _lock:
        item = _state.get(rule_id)
        if item is None:
            return
        item["running"] = False
        item["phase"] = "done"
        item["current"] = ""
        item["finished_at"] = datetime.now().isoformat(timespec="seconds")
        if message:
            item["message"] = message


def snapshot(rule_id: int = 0) -> list[dict]:
    """取进度快照。rule_id 为 0 时返回全部。"""
    with _lock:
        if rule_id:
            item = _state.get(rule_id)
            return [dict(item)] if item else []
        return [dict(v) for v in _state.values()]


def is_running(rule_id: int) -> bool:
    with _lock:
        item = _state.get(rule_id)
        return bool(item and item["running"])


def clear(rule_id: int) -> None:
    with _lock:
        _state.pop(rule_id, None)
