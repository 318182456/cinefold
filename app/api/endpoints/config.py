"""配置、日志、任务管理。"""
from __future__ import annotations

from dataclasses import fields

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.endpoints import get_current_user
from app.core.config import SENSITIVE_KEYS, Settings, get_settings, save_settings
from app.core.version import APP_VERSION
from app.schemas.reponse import ResponseEntity
from app.utils.log import read_logs

router = APIRouter(tags=["config"])

VERSION = APP_VERSION


class ConfigRequest(BaseModel):
    config: dict


@router.get("/config")
def get_config(current_user: str = Depends(get_current_user)):
    """返回配置，敏感字段已打码。"""
    return ResponseEntity.ok(get_settings().to_safe_dict())


@router.post("/config")
def save_config(body: ConfigRequest, current_user: str = Depends(get_current_user)):
    """保存配置。打码占位的字段会被跳过，避免把掩码写回去。"""
    valid_keys = {f.name for f in fields(Settings)}
    updates: dict = {}

    for key, value in (body.config or {}).items():
        if key not in valid_keys:
            continue
        # 前端回传的掩码说明用户没改这一项
        if key in SENSITIVE_KEYS and isinstance(value, str) and set(value) == {"*"}:
            continue
        updates[key.upper()] = value

    if not updates:
        return ResponseEntity.ok(message="没有需要更新的配置")

    save_settings(updates)
    get_settings(reload=True)

    # 定时任务表达式可能变了，重建调度器
    from app.scheduler import restart_scheduler
    restart_scheduler()

    return ResponseEntity.ok(message=f"已更新 {len(updates)} 项配置")


@router.get("/version")
def get_version():
    return ResponseEntity.ok({"version": VERSION})


@router.get("/logs")
def get_logs(
    lines: int = 200,
    keyword: str = "",
    current_user: str = Depends(get_current_user),
):
    return ResponseEntity.ok({"logs": read_logs(lines, keyword)})


@router.get("/cron")
def list_cron(current_user: str = Depends(get_current_user)):
    from app.scheduler import list_jobs
    return ResponseEntity.ok({"jobs": list_jobs()})


@router.post("/task")
def run_task(job_id: str, current_user: str = Depends(get_current_user)):
    """手动触发定时任务。"""
    from app.scheduler import push_job
    if not push_job(job_id):
        return ResponseEntity.fail(f"未知任务 {job_id}", code=404)
    return ResponseEntity.ok(message="任务已触发")


@router.get("/test")
def test_connection(target: str, current_user: str = Depends(get_current_user)):
    """测试外部服务连接。target: qbittorrent / transmission / emby / ..."""
    testers = {
        "qbittorrent": lambda: _import_client("qbittorrent", "QBitTorrentClient"),
        "transmission": lambda: _import_client("transmission", "TransmissionClient"),
        "emby": lambda: _import_server("emby", "Emby"),
        "jellyfin": lambda: _import_server("jellyfin", "Jellyfin"),
        "plex": lambda: _import_server("plex", "Plex"),
        "telegram": lambda: _import_notifier("telegram", "TelegramNotifier"),
        "wechat": lambda: _import_notifier("wechat", "WeChatNotifier"),
        "mteam": lambda: _import_site("mteam", "MTeam"),
    }

    factory = testers.get((target or "").lower())
    if factory is None:
        return ResponseEntity.fail(f"不支持的测试目标 {target}", code=400)

    try:
        ok, message = factory().test_connection()
        return ResponseEntity.ok({"success": ok, "message": message})
    except Exception as exc:
        return ResponseEntity.ok({"success": False, "message": str(exc)})


def _import_client(module: str, cls: str):
    import importlib
    return getattr(importlib.import_module(f"app.modules.downloadclient.{module}"), cls)()


def _import_server(module: str, cls: str):
    import importlib
    return getattr(importlib.import_module(f"app.modules.mediaserver.{module}"), cls)()


def _import_notifier(module: str, cls: str):
    import importlib
    return getattr(importlib.import_module(f"app.modules.notify.{module}"), cls)()


def _import_site(module: str, cls: str):
    import importlib
    return getattr(importlib.import_module(f"app.modules.ptsite.{module}"), cls)()
