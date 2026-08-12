"""agent 可调用的工具集。

除 propose_action 外全是只读的，直接查库或读现成的服务层结果。

propose_action 也不直接动手：它只登记一个待确认操作，用户在面板上点确认才
真正执行（见 actions.py）。对话入口很容易被问出"把进度低的都删了"，而模型
完全可能把筛选条件理解错，删除又不可逆 —— 让人来当最后一道闸。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, or_, select

from app.database.models import Actor, Code, CodeStatus, History
from app.database.session import session_scope

# 单个工具最多返回多少条记录。返回值要塞回模型上下文，条数一多既费 token
# 又容易把真正有用的信息挤出去
ROW_LIMIT = 20

STATUS_LABEL = {
    CodeStatus.NONE: "未订阅",
    CodeStatus.SUBSCRIBED: "已订阅待资源",
    CodeStatus.DOWNLOADING: "下载中",
    CodeStatus.DOWNLOADED: "已下载",
    CodeStatus.COMPLETED: "已入库",
    CodeStatus.FAILED: "失败",
}

STATUS_BY_NAME = {
    "none": CodeStatus.NONE,
    "subscribed": CodeStatus.SUBSCRIBED,
    "downloading": CodeStatus.DOWNLOADING,
    "downloaded": CodeStatus.DOWNLOADED,
    "completed": CodeStatus.COMPLETED,
    "failed": CodeStatus.FAILED,
}


def _code_brief(row: Code) -> dict:
    """番号的精简视图。封面、剧照这类字段对话里用不上，不带出去。"""
    return {
        "code": row.code,
        "title": row.cn_title or row.title or "",
        "status": STATUS_LABEL.get(row.status, str(row.status)),
        "release_date": row.release_date or "",
        "producer": row.producer or "",
        "casts": row.casts or "",
        "update_time": row.update_time.strftime("%Y-%m-%d %H:%M") if row.update_time else "",
    }


def tool_overview(_: dict) -> dict:
    """总体情况：各状态计数 + 最近活跃度。"""
    from app import services

    stats = services.dashboard_stats()

    with session_scope() as session:
        since = datetime.now() - timedelta(days=7)
        stats["added_last_7d"] = session.scalar(
            select(func.count()).select_from(Code).where(Code.create_time >= since)
        ) or 0
        stats["downloaded_last_7d"] = session.scalar(
            select(func.count()).select_from(History).where(History.create_time >= since)
        ) or 0
    return stats


def tool_query_codes(args: dict) -> dict:
    """按状态/关键词/演员查番号列表。"""
    status_arg = (args.get("status") or "").strip().lower()
    keyword = (args.get("keyword") or "").strip()
    limit = min(int(args.get("limit") or 10), ROW_LIMIT)

    with session_scope() as session:
        query = select(Code)
        if status_arg and status_arg in STATUS_BY_NAME:
            query = query.where(Code.status == STATUS_BY_NAME[status_arg])
        if keyword:
            like = f"%{keyword}%"
            query = query.where(
                or_(
                    Code.code.ilike(like),
                    Code.title.ilike(like),
                    Code.cn_title.ilike(like),
                    Code.casts.ilike(like),
                    Code.producer.ilike(like),
                )
            )

        total = session.scalar(
            select(func.count()).select_from(query.subquery())
        ) or 0
        rows = session.scalars(
            query.order_by(Code.update_time.desc()).limit(limit)
        ).all()
        return {"total": total, "returned": len(rows), "items": [_code_brief(r) for r in rows]}


def tool_code_detail(args: dict) -> dict:
    """单个番号的完整情况，含下载历史。"""
    from app.utils import get_true_code

    raw = (args.get("code") or "").strip()
    if not raw:
        return {"error": "未提供番号"}

    code = get_true_code(raw) or raw

    with session_scope() as session:
        row = session.scalar(select(Code).where(Code.code == code.upper()))
        if row is None:
            # 番号规范化后仍可能对不上（别名、带后缀），退一步做模糊匹配
            row = session.scalar(select(Code).where(Code.code.ilike(f"%{raw}%")))
        if row is None:
            return {"found": False, "code": code, "message": "库里没有这个番号"}

        detail = _code_brief(row)
        detail.update({
            "found": True,
            "series": row.series or "",
            "genres": row.genres or "",
            "duration": row.duration or "",
            "star": row.star,
            "create_time": row.create_time.strftime("%Y-%m-%d %H:%M") if row.create_time else "",
        })

        history = session.scalars(
            select(History).where(History.code == row.code)
            .order_by(History.create_time.desc()).limit(5)
        ).all()
        detail["download_history"] = [
            {
                "save_path": h.save_path or "",
                "time": h.create_time.strftime("%Y-%m-%d %H:%M") if h.create_time else "",
            }
            for h in history
        ]
    return detail


def tool_list_actors(args: dict) -> dict:
    """订阅中的演员。"""
    limit = min(int(args.get("limit") or 20), ROW_LIMIT)
    keyword = (args.get("keyword") or "").strip()

    with session_scope() as session:
        query = select(Actor)
        if keyword:
            like = f"%{keyword}%"
            query = query.where(or_(Actor.name.ilike(like), Actor.name_2.ilike(like)))

        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = session.scalars(
            query.order_by(Actor.update_time.desc()).limit(limit)
        ).all()
        return {
            "total": total,
            "items": [
                {"name": r.name, "alias": r.name_2 or "", "limit_date": r.limit_date or ""}
                for r in rows
            ],
        }


def tool_list_tasks(_: dict) -> dict:
    """定时任务及下次执行时间。"""
    from app.scheduler import list_jobs
    jobs = list_jobs()
    return {"total": len(jobs), "jobs": jobs, "running": bool(jobs)}


def tool_read_logs(args: dict) -> dict:
    """查最近日志，可按关键词过滤。排查"为什么没下载"这类问题靠它。"""
    from app.utils.log import read_logs

    keyword = (args.get("keyword") or "").strip()
    # 日志行普遍很长，条数给大了会挤爆上下文
    lines = min(int(args.get("lines") or 50), 120)
    logs = read_logs(lines, keyword)
    return {"returned": len(logs), "keyword": keyword, "logs": logs}


def tool_list_downloads(args: dict) -> dict:
    """下载器里的实时任务。番号库只记状态，进度和卡没卡得问下载器。"""
    from app.modules.downloadclient import get_download_client, list_configured_clients

    client_name = (args.get("client") or "").strip().lower()
    keyword = (args.get("keyword") or "").strip().lower()
    state = (args.get("state") or "").strip().lower()
    try:
        max_progress = float(args.get("max_progress"))
    except (TypeError, ValueError):
        max_progress = None
    limit = min(int(args.get("limit") or 20), ROW_LIMIT)

    names = [client_name] if client_name else list_configured_clients()
    items: list[dict] = []
    errors: list[str] = []

    for name in names:
        client = get_download_client(name)
        if client is None:
            continue
        try:
            for row in client.monitor_torrent():
                items.append({
                    "client": name,
                    "hash": row.get("hash", ""),
                    "name": row.get("name", ""),
                    # 进度按百分比给，模型对 0.07 与 7% 的理解稳定性差别很大
                    "progress_percent": round((row.get("progress") or 0) * 100, 1),
                    "state": row.get("state", ""),
                    "completed": bool(row.get("completed")),
                })
        except Exception as exc:
            logger.warning(f"读取 {name} 任务列表异常: {exc}")
            errors.append(f"{name}: {exc}")

    if keyword:
        items = [i for i in items if keyword in i["name"].lower()]
    if state:
        items = [i for i in items if state in i["state"].lower()]
    if max_progress is not None:
        items = [i for i in items if i["progress_percent"] <= max_progress]

    items.sort(key=lambda i: i["progress_percent"])
    total = len(items)
    result = {
        "total": total,
        "returned": min(total, limit),
        "items": items[:limit],
        "clients": names,
    }
    if errors:
        result["errors"] = errors
    if total > limit:
        result["note"] = f"还有 {total - limit} 个未列出，可缩小筛选范围"
    return result


def _lookup_torrents(hashes: list[str], client_name: str = "") -> dict[str, dict]:
    """按 hash 精确查任务，返回 {小写 hash: {name, progress_percent, client}}。"""
    from app.modules.downloadclient import get_download_client, list_configured_clients

    names = [client_name] if client_name else list_configured_clients()
    found: dict[str, dict] = {}

    for name in names:
        client = get_download_client(name)
        if client is None:
            continue
        try:
            rows = client.monitor_torrent(hashes)
        except Exception as exc:
            logger.warning(f"按 hash 查询 {name} 任务异常: {exc}")
            continue
        for row in rows:
            key = (row.get("hash") or "").lower()
            if key and key not in found:
                found[key] = {
                    "client": name,
                    "name": row.get("name", ""),
                    "progress_percent": round((row.get("progress") or 0) * 100, 1),
                }
    return found


def tool_propose_action(args: dict) -> dict:
    """登记一个待用户确认的下载器操作。

    这里只登记不执行 —— 助手可能把筛选条件理解错，删除又不可逆，
    所以必须由用户在面板上点确认才真正动手。
    """
    from .actions import ACTIONS, create_proposal

    action = (args.get("action") or "").strip().lower()
    if action not in ACTIONS:
        return {"error": f"不支持的操作 {action}，可用：{', '.join(ACTIONS)}"}

    raw_hashes = args.get("hashes") or []
    if isinstance(raw_hashes, str):
        raw_hashes = [h.strip() for h in raw_hashes.split(",")]
    hashes = [h for h in (raw_hashes or []) if h]
    if not hashes:
        return {"error": "必须给出要操作的任务 hash，先用 list_downloads 查"}
    if len(hashes) > ROW_LIMIT:
        return {"error": f"一次最多操作 {ROW_LIMIT} 个任务，请分批"}

    client_name = (args.get("client") or "").strip().lower()

    # 用当前真实的任务名回填，避免助手把名字记错，用户看到的确认信息才可信。
    # 只查这几个 hash —— 下载器里挂着几千个种子是常态，全量拉一遍要几十秒
    known = _lookup_torrents(hashes, client_name)

    targets = []
    for value in hashes:
        info = known.get(value.lower())
        targets.append({
            "hash": value,
            "name": info["name"] if info else "(下载器中未找到该任务)",
            "progress_percent": info["progress_percent"] if info else None,
            "found": bool(info),
        })

    proposal = create_proposal(action, targets, client_name)
    return {
        "proposal": proposal,
        "message": (
            f"已生成待确认操作：{proposal['label']}，涉及 {len(targets)} 个任务。"
            "请告知用户在面板上点确认后才会执行，不要声称已经执行完毕。"
        ),
    }


def tool_check_config(_: dict) -> dict:
    """配置体检：哪些必要项没配。密钥一律只报是否已配，不回显内容。"""
    from app.core.config import get_settings

    settings = get_settings()

    downloaders = {
        "qbittorrent": bool(settings.qbittorrent_url),
        "transmission": bool(settings.transmission_url),
        "thunder": bool(settings.thunder_authorization),
    }
    sources = {
        "mteam": bool(settings.mteam_api_key),
        "rousi": bool(settings.rousi_password or settings.rousi_token),
        "ptt": bool(settings.ptt_cookie),
        "nicept": bool(settings.nicept_cookie),
        "bt": bool(settings.bt_url),
    }
    media = {
        "emby": bool(settings.emby_url),
        "jellyfin": bool(settings.jellyfin_url),
        "plex": bool(settings.plex_url),
    }
    notify = {
        "telegram": bool(settings.telegram_bot_token),
        "wechat": bool(settings.wechat_corp_id),
    }

    problems = []
    if not any(downloaders.values()):
        problems.append("没有配置任何下载器，搜到资源也推不出去")
    if not any(sources.values()):
        problems.append("没有配置任何资源站或 BT 源，搜不到种子")

    return {
        "downloaders": downloaders,
        "sources": sources,
        "media_servers": media,
        "notify": notify,
        "proxy": bool(settings.proxy),
        "bypass_service": bool(settings.bypass_url),
        "problems": problems,
    }


# OpenAI function calling 的工具声明。名字与 REGISTRY 的键一一对应
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "overview",
            "description": (
                "获取系统总体情况：番号总数、各订阅状态计数、演员数、"
                "下载历史数、最近 7 天新增与下载量。问『现在什么情况』先调它。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_codes",
            "description": "按状态或关键词（番号/标题/演员/厂牌）查询番号列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": list(STATUS_BY_NAME.keys()),
                        "description": "订阅状态筛选，留空为不限",
                    },
                    "keyword": {"type": "string", "description": "模糊关键词"},
                    "limit": {"type": "integer", "description": f"返回条数，最大 {ROW_LIMIT}"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "code_detail",
            "description": "查单个番号的详细情况与下载历史。用户问某个具体番号时用。",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "番号，如 ABC-123"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_actors",
            "description": "查询已订阅的演员列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "演员名关键词"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "查询定时任务列表与下次执行时间，判断调度器是否在跑。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_logs",
            "description": (
                "读最近的运行日志，可按关键词过滤（如番号、'错误'、'失败'）。"
                "排查『为什么没下载』『哪里报错』时用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "过滤关键词"},
                    "lines": {"type": "integer", "description": "返回行数，最大 120"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_downloads",
            "description": (
                "查下载器（qBittorrent / Transmission）里的实时任务：进度、状态。"
                "问『下载卡住了吗』『哪些任务进度低』，或要对任务动手之前，都先用它拿 hash。"
                "结果按进度升序。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "client": {
                        "type": "string",
                        "enum": ["qbittorrent", "transmission"],
                        "description": "只查某个下载器，留空查全部",
                    },
                    "keyword": {"type": "string", "description": "任务名关键词"},
                    "state": {"type": "string", "description": "状态关键词，如 stalled、downloading、paused"},
                    "max_progress": {
                        "type": "number",
                        "description": "只要进度不超过该百分比的任务，如 10 表示 10% 以下",
                    },
                    "limit": {"type": "integer", "description": f"返回条数，最大 {ROW_LIMIT}"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_action",
            "description": (
                "对下载器任务发起操作。这一步只生成待确认项，不会真的执行 —— "
                "用户在面板上点确认后才生效，所以调完必须告诉用户去确认，"
                "绝不能说『已经暂停/删除好了』。hash 必须来自 list_downloads。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["pause", "resume", "recheck", "reannounce", "delete", "delete_with_files"],
                        "description": (
                            "delete 只删任务保留文件；delete_with_files 连磁盘文件一起删，"
                            "不可逆，用户没明确说要删文件时不要选它"
                        ),
                    },
                    "hashes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要操作的任务 hash，来自 list_downloads",
                    },
                    "client": {
                        "type": "string",
                        "enum": ["qbittorrent", "transmission"],
                        "description": "任务所在的下载器，留空则全部下载器都试",
                    },
                },
                "required": ["action", "hashes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_config",
            "description": (
                "检查配置完整性：下载器、资源站、媒体库、通知是否已配置，"
                "并列出明显的问题。只返回是否配置，不返回密钥内容。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

REGISTRY = {
    "overview": tool_overview,
    "query_codes": tool_query_codes,
    "code_detail": tool_code_detail,
    "list_actors": tool_list_actors,
    "list_tasks": tool_list_tasks,
    "read_logs": tool_read_logs,
    "list_downloads": tool_list_downloads,
    "propose_action": tool_propose_action,
    "check_config": tool_check_config,
}

# 会产生待确认提案的工具。答复里要把提案带给前端，靠它识别
PROPOSAL_TOOLS = {"propose_action"}


def call_tool(name: str, arguments: str | dict) -> str:
    """执行工具并返回 JSON 字符串，供回填进对话。

    工具自身出错不能中断整轮对话，把错误也序列化回去，让模型自己决定
    是换个工具还是直接告诉用户查不到。
    """
    func = REGISTRY.get(name)
    if func is None:
        return json.dumps({"error": f"未知工具 {name}"}, ensure_ascii=False)

    if isinstance(arguments, str):
        try:
            args = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return json.dumps({"error": "参数不是合法 JSON"}, ensure_ascii=False)
    else:
        args = arguments or {}

    try:
        result = func(args)
    except Exception as exc:
        logger.warning(f"agent 工具 {name} 执行异常: {exc}")
        return json.dumps({"error": f"工具执行失败: {exc}"}, ensure_ascii=False)

    logger.debug(f"agent 工具 {name} 参数={args}")
    return json.dumps(result, ensure_ascii=False, default=str)
