"""agent 可调用的工具集。

每个工具都是只读的，直接查库或读现成的服务层结果，不做任何写操作——
对话入口很容易被问出"帮我把这个删了"，工具层不给这个能力最省心。
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
    "check_config": tool_check_config,
}


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
