"""下载器写操作。

助手不直接动手：它只能"提案"，拿到一个待确认的 action，用户在面板上点确认
才真正执行。理由是模型可能把筛选条件理解错（"进度 10% 以下"错成"10 个以内"），
删除又不可逆，所以让人来当最后一道闸。

提案存在内存里，带过期时间。重启即失效 —— 待确认的操作本就该是秒级的事，
落库反而会让重启后冒出一堆陈旧提案。
"""
from __future__ import annotations

import threading
import time
import uuid

from loguru import logger

# 提案多久过期。够用户看清要做什么，又不至于放到情况早已变化
PROPOSAL_TTL = 300

# 动作 → (人话, 是否不可逆)
ACTIONS = {
    "pause": ("暂停", False),
    "resume": ("恢复", False),
    "recheck": ("重新检查", False),
    "reannounce": ("强制汇报", False),
    "delete": ("删除任务（保留文件）", True),
    "delete_with_files": ("删除任务并删除文件", True),
}

_lock = threading.Lock()
_proposals: dict[str, dict] = {}


def _sweep() -> None:
    """清掉过期提案。调用方需持锁。"""
    now = time.time()
    for key in [k for k, v in _proposals.items() if v["expire_at"] <= now]:
        _proposals.pop(key, None)


def create_proposal(action: str, targets: list[dict], client_name: str = "") -> dict:
    """登记一个待确认操作，返回给前端展示。"""
    label, destructive = ACTIONS[action]
    proposal_id = uuid.uuid4().hex
    record = {
        "id": proposal_id,
        "action": action,
        "label": label,
        "destructive": destructive,
        "client": client_name,
        "targets": targets,
        "expire_at": time.time() + PROPOSAL_TTL,
    }
    with _lock:
        _sweep()
        _proposals[proposal_id] = record

    logger.info(
        f"agent 登记待确认操作 {action}（{len(targets)} 个任务），id={proposal_id}"
    )
    # expire_at 是内部字段，不必给前端
    return {k: v for k, v in record.items() if k != "expire_at"}


def take_proposal(proposal_id: str) -> dict | None:
    """取出并销毁提案。一次性，避免重复点确认执行两遍。"""
    with _lock:
        _sweep()
        return _proposals.pop(proposal_id, None)


def _clients_for(name: str = ""):
    """要操作的下载器实例列表。

    助手指定了就只动那一个；没指定则所有已配置的都试 —— 番号可能推到了
    任意一个下载器，hash 在哪个里面只有问了才知道。
    """
    from app.modules.downloadclient import get_download_client, list_configured_clients

    names = [name] if name else list_configured_clients()
    out = []
    for item in names:
        client = get_download_client(item)
        if client is not None:
            out.append((item, client))
    return out


def _clear_history(hashes: list[str]) -> None:
    """删完种子把下载历史一并清掉。

    留着的话订阅任务会认为该番号已下载而跳过，这个番号就再也不会被重新下。
    与联动删除（services/medialink.py）的处理保持一致。
    """
    from app.database.models import History
    from app.database.session import session_scope

    try:
        with session_scope() as session:
            for value in hashes:
                row = session.get(History, value)
                if row is None:
                    # hash 大小写可能与库里不一致，再按小写找一次
                    row = session.get(History, value.lower())
                if row is not None:
                    session.delete(row)
    except Exception as exc:
        # 种子已经删了，历史没清干净不该让整个操作报失败
        logger.warning(f"清理下载历史失败: {exc}")


def execute(proposal_id: str) -> dict:
    """执行已确认的操作。"""
    record = take_proposal(proposal_id)
    if record is None:
        return {"ok": False, "message": "操作已过期或已执行，请重新提问"}

    action = record["action"]
    hashes = [t["hash"] for t in record["targets"] if t.get("hash")]
    if not hashes:
        return {"ok": False, "message": "没有可操作的任务"}

    done: list[str] = []
    errors: list[str] = []

    for name, client in _clients_for(record.get("client") or ""):
        try:
            if action == "delete":
                done += client.delete_torrent(hashes, delete_files=False)
            elif action == "delete_with_files":
                done += client.delete_torrent(hashes, delete_files=True)
            else:
                controller = getattr(client, "control_torrent", None)
                if controller is None:
                    # 迅雷没有这些操作，不算错误
                    continue
                done += controller(action, hashes)
        except Exception as exc:
            logger.warning(f"{name} 执行 {action} 异常: {exc}")
            errors.append(f"{name}: {exc}")

    # 去重：同一个 hash 可能同时存在于多个下载器
    done = list(dict.fromkeys(done))

    if not done:
        message = "没有任务被操作到" + (f"（{'; '.join(errors)}）" if errors else "，可能已不在下载器里")
        return {"ok": False, "message": message, "affected": 0}

    if action in ("delete", "delete_with_files"):
        _clear_history(done)

    logger.info(f"agent 已执行 {action}，影响 {len(done)} 个任务")
    return {
        "ok": True,
        "message": f"已{record['label']} {len(done)} 个任务",
        "affected": len(done),
        "hashes": done,
    }
