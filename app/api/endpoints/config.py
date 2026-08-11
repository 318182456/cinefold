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

    # 接收方式或 token 可能改了，按新配置重挂长轮询
    from app.modules.notify.tgpolling import restart_polling
    restart_polling()

    # 账号密码可能换了，丢掉旧账号换来的 token
    from app.modules.ptsite.rousi import Rousi
    Rousi.reset_token_cache()

    # 域名或 Cookie 改了就该立刻重试，不必等熔断冷却
    from app.modules.ptsite.nexus import NexusSite
    NexusSite.reset_breakers()

    # issuer 可能换了，端点配置得重新探测
    from app.modules.auth.oidc import reset_discovery_cache
    reset_discovery_cache()

    return ResponseEntity.ok(message=f"已更新 {len(updates)} 项配置")


@router.get("/version")
def get_version():
    return ResponseEntity.ok({"version": VERSION})


@router.get("/version/check")
def check_version(
    refresh: bool = False, current_user: str = Depends(get_current_user)
):
    """对比镜像仓库里的最新版本。查不到时 checked 为 False，前端不提示。"""
    from app.utils.updatecheck import check_update
    return ResponseEntity.ok(check_update(use_cache=not refresh))


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
        "rousi": lambda: _import_site("rousi", "Rousi"),
        "ptt": lambda: _import_site("ptt", "PTT"),
        "nicept": lambda: _import_site("nicept", "NicePT"),
    }

    factory = testers.get((target or "").lower())
    if factory is None:
        return ResponseEntity.fail(f"不支持的测试目标 {target}", code=400)

    try:
        instance = factory()
        # 下载器/媒体服务器/通知用 test_connection，PT 站点用 check_status
        tester = getattr(instance, "test_connection", None) or getattr(
            instance, "check_status", None
        )
        if tester is None:
            return ResponseEntity.ok(
                {"success": False, "message": f"{target} 不支持连接测试"}
            )
        ok, message = tester()
        return ResponseEntity.ok({"success": ok, "message": message})
    except Exception as exc:
        return ResponseEntity.ok({"success": False, "message": str(exc)})


@router.get("/ptsites")
def list_ptsites(current_user: str = Depends(get_current_user)):
    """已配置的 PT 站点名，供主站下拉使用。"""
    from app.modules import ptsite
    return ResponseEntity.ok({"sites": [site.name for site in ptsite.get_sites()]})


WEBHOOK_PATH = "/api/v1/message"


def _webhook_url(domain: str) -> str:
    """外网地址 → 完整回调地址。用户通常只填域名。"""
    domain = (domain or "").strip().rstrip("/")
    if not domain:
        return ""
    if not domain.startswith(("http://", "https://")):
        domain = f"https://{domain}"
    if domain.endswith(WEBHOOK_PATH):
        return domain
    return f"{domain}{WEBHOOK_PATH}"


@router.get("/telegram/receive")
def telegram_receive_status(current_user: str = Depends(get_current_user)):
    """当前上行消息的接收状态，供前端展示。"""
    from app.modules.notify.telegram import TelegramNotifier
    from app.modules.notify.tgpolling import is_polling

    settings = get_settings()
    info = TelegramNotifier().get_webhook_info()
    return ResponseEntity.ok({
        "mode": settings.telegram_receive_mode,
        "polling_running": is_polling(),
        "webhook_url": info.get("url", ""),
        "pending_update_count": info.get("pending_update_count", 0),
        "last_error_message": info.get("last_error_message", ""),
        # Telegram 会一直保留最后一次错误，直到下次回调成功才清掉。
        # 带上时间戳，前端才能说明这是历史错误而非当前故障
        "last_error_date": info.get("last_error_date", 0),
        "suggest_url": _webhook_url(settings.external_domain),
    })


class WebhookRequest(BaseModel):
    # 留空则用配置里的外网地址
    url: str = ""


@router.post("/telegram/webhook")
def set_telegram_webhook(
    body: WebhookRequest, current_user: str = Depends(get_current_user)
):
    """设置 webhook。地址留空时取配置里的外网地址。"""
    from app.modules.notify.telegram import TelegramNotifier

    settings = get_settings()
    url = _webhook_url(body.url or settings.external_domain)
    if not url:
        return ResponseEntity.ok(
            {"success": False, "message": "请先填写外网访问地址"}
        )
    if not url.startswith("https://"):
        return ResponseEntity.ok(
            {"success": False, "message": "Telegram 要求 webhook 必须是 HTTPS 地址"}
        )

    ok, message = TelegramNotifier().set_webhook(url)
    if ok:
        message = f"{message}：{url}"
    return ResponseEntity.ok({"success": ok, "message": message, "url": url})


@router.delete("/telegram/webhook")
def delete_telegram_webhook(current_user: str = Depends(get_current_user)):
    """删除 webhook。切回 polling 前用。"""
    from app.modules.notify.telegram import TelegramNotifier

    ok, message = TelegramNotifier().delete_webhook()
    return ResponseEntity.ok({"success": ok, "message": message})


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
