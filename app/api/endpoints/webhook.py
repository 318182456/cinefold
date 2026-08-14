"""外部 webhook 接收。

- POST /webhook/scrape  刮削工具（MDCng 等）刮削完成回调，登记硬链接关联
- POST /webhook/emby    Emby / Jellyfin 删除影片回调，联动清理种子与文件

两个端点都不走登录态（外部系统无法带 Cookie），改用 X-Cinefold-Token
校验。删除端点会真的删文件，务必配上密钥。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, Request
from loguru import logger
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.schemas.reponse import ResponseEntity
from app.services.medialink import handle_media_deleted, register_scrape
from app.utils import get_true_code

router = APIRouter(prefix="/webhook", tags=["webhook"])

# Emby/Jellyfin 表示"条目已删除"的事件名，各版本与插件写法不一
DELETE_EVENTS = {
    "item.remove", "item.deleted", "library.deleted",
    "itemremoved", "itemdeleted",
}


def _check_token(request: Request) -> bool:
    """校验 webhook 密钥。未配置密钥时放行。"""
    expected = (get_settings().medialink_webhook_token or "").strip()
    if not expected:
        return True
    got = (
        request.headers.get("x-cinefold-token")
        or request.query_params.get("token")
        or ""
    ).strip()
    if got != expected:
        logger.warning(f"webhook 密钥校验失败，来源 {request.client.host if request.client else '未知'}")
        return False
    return True


# JSON 里的合法转义中，这几个不可能是 Windows 路径分隔符的本意
_KEEP_ESCAPES = r'["\\/u]'
# 解析结果里出现这些控制字符，说明原文是被误当转义处理的路径分隔符
_CONTROL_CHARS = "\t\b\n\r\f"


def _repair_backslashes(text: str) -> str:
    """把当作路径分隔符用的反斜杠补成双写。"""
    return re.sub(rf"\\(?!{_KEEP_ESCAPES})", r"\\\\", text)


def _has_control_chars(data: object) -> bool:
    """递归判断解析结果里有没有可疑控制字符。"""
    if isinstance(data, str):
        return any(ch in data for ch in _CONTROL_CHARS)
    if isinstance(data, dict):
        return any(_has_control_chars(v) for v in data.values())
    if isinstance(data, list):
        return any(_has_control_chars(v) for v in data)
    return False


async def _parse_body(request: Request) -> dict:
    """解析请求体，尽量宽容。

    刮削工具的 body 模板由用户手写，Windows 路径里的反斜杠基本不会转义，
    这会以两种方式出错，都得处理：

    1) 直接把 JSON 打坏 —— "D:\\ABS" 里 \\A 不是合法转义，解析抛异常
    2) 更隐蔽：JSON 合法但语义全错 —— "D:\\test" 里 \\t 被解析成制表符，
       路径静默变成 "D:<TAB>est"。这种情况不抛异常，只能靠事后检查控制
       字符发现，否则拿着错路径去删文件就危险了。

    所以两条路都走：解析失败时修复重试；解析成功但含控制字符时，同样用
    修复后的原文重解一次。
    """
    raw = await request.body()
    if not raw:
        return {}
    text = raw.decode("utf-8", errors="replace")

    data: object | None = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = json.loads(_repair_backslashes(text))
            logger.info("webhook body 含未转义反斜杠，已修复后解析")
        except json.JSONDecodeError as exc:
            logger.error(
                f"webhook body 不是合法 JSON: {exc}; 原文前 200 字: {text[:200]}"
            )
            # 退回表单解析，MDCng 也可能以 form 方式提交
            try:
                form = await request.form()
                return {k: str(v) for k, v in form.items()}
            except Exception:
                return {}
    else:
        if _has_control_chars(data):
            try:
                data = json.loads(_repair_backslashes(text))
                logger.info(
                    "webhook body 中的反斜杠被误解析为控制字符（Windows 路径），"
                    "已按字面反斜杠重新解析"
                )
            except json.JSONDecodeError:
                # 修复反而解不出来，说明原本就是有意的控制字符，保留首次结果
                pass

    return data if isinstance(data, dict) else {}


def _pick(data: dict, *keys: str) -> str:
    """按优先级取第一个非空字段，兼容不同工具的命名。"""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# ----------------------------------------------------------------------
@router.post("/scrape")
async def scrape_webhook(request: Request):
    """刮削完成回调。

    期望字段（缺失的会尽量兜底）：
        event        finished / failed
        number       番号
        source_path  源文件完整路径
        link_path    刮削产物路径，可选。给了就当快速路径用
    """
    if not _check_token(request):
        return ResponseEntity.fail("密钥校验失败", code=403)

    data = await _parse_body(request)
    if not data:
        return ResponseEntity.fail("请求体为空或不是合法 JSON", code=400)

    event = _pick(data, "event", "status").lower()
    if event and event not in ("finished", "success", "ok", "done"):
        logger.info(f"刮削回调事件为 {event}，非成功状态，忽略")
        return ResponseEntity.ok({"ignored": True, "event": event})

    number = _pick(data, "number", "code", "id")
    source_path = _pick(data, "source_path", "sourcepath", "file", "path")
    link_path = _pick(data, "link_path", "target_path", "dest_path")

    if not number or not source_path:
        logger.warning(f"刮削回调缺少 number 或 source_path，已忽略。收到字段: {list(data)}")
        return ResponseEntity.fail("缺少 number 或 source_path", code=400)

    code = get_true_code(number) or number

    # stat + rglob 是阻塞 IO，别占着事件循环
    links = await run_in_threadpool(register_scrape, code, source_path, link_path)

    return ResponseEntity.ok({
        "code": code,
        "source_path": source_path,
        "links": links,
    })


@router.post("/emby")
async def emby_webhook(request: Request):
    """Emby / Jellyfin 删除影片回调，联动删除种子与文件。

    传 dry_run=1 可只查看会删什么而不实际删除，建议接入前先试一次。
    """
    if not _check_token(request):
        return ResponseEntity.fail("密钥校验失败", code=403)

    data = await _parse_body(request)
    if not data:
        return ResponseEntity.fail("请求体为空或不是合法 JSON", code=400)

    event = _pick(data, "Event", "event", "NotificationType", "notification_type").lower()
    if event and event.replace("_", ".") not in DELETE_EVENTS:
        logger.info(f"Emby 回调事件为 {event}，非删除事件，忽略")
        return ResponseEntity.ok({"ignored": True, "event": event})

    # Emby 原生 webhook 是嵌套的 {"Item": {"Path": ...}}，模板化的则是平铺
    item = data.get("Item") if isinstance(data.get("Item"), dict) else {}
    link_path = _pick(data, "link_path", "path", "Path") or _pick(item, "Path", "path")
    number = (
        _pick(data, "number", "code")
        or _pick(item, "OriginalTitle", "Name")
    )
    code = get_true_code(number) if number else ""

    if not link_path and not code:
        logger.warning(f"Emby 回调缺少路径与番号，无法定位，已忽略。收到字段: {list(data)}")
        return ResponseEntity.fail("缺少 path 或 number", code=400)

    # 目录级删除事件：Emby 删完影片后，空掉的演员/系列目录也会各来一条回调，
    # 路径指向目录、番号为空。这类事件本就不该有关联记录，走下去只会在日志里
    # 刷一堆「未找到关联记录」的 WARNING，把真正失效的联动淹掉。
    #
    # 判据是「没有番号 + 路径没有影片扩展名」：两个条件都满足才算目录。
    # 只看扩展名会误伤 .strm，只看番号会误伤命名不规范的影片文件
    if not code and link_path:
        from app.services.medialink import VIDEO_SUFFIXES

        if Path(link_path).suffix.lower() not in VIDEO_SUFFIXES:
            logger.info(f"Emby 回调为目录级删除事件，忽略: {link_path}")
            return ResponseEntity.ok({"ignored": True, "reason": "目录级事件"})

    dry_run = str(
        request.query_params.get("dry_run") or data.get("dry_run") or ""
    ).lower() in ("1", "true", "yes")

    result = await run_in_threadpool(
        handle_media_deleted, link_path, code or "", dry_run
    )
    return ResponseEntity.ok(result.as_dict())
