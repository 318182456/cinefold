"""消息回调。接收 Telegram / 企业微信的上行消息，支持简单指令。"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from loguru import logger

from app.core.config import get_settings
from app.schemas.reponse import ResponseEntity
from app.utils import get_true_code
from app.utils.codefilter import extract_subscribable_codes, normalize_explicit_codes

router = APIRouter(tags=["message"])

HELP_TEXT = (
    "可用指令：\n"
    "/sub 番号 — 订阅，可一次给多个\n"
    "/cancel 番号 — 取消订阅，可一次给多个\n"
    "/search 番号 — 搜索种子\n"
    "/status — 查看统计\n"
    "直接发送番号等同于 /sub"
)


def _filter_options() -> dict:
    """当前生效的番号过滤配置。"""
    settings = get_settings()
    return {
        "allow_prefixes": settings.msg_allow_prefixes,
        "block_prefixes": settings.msg_block_prefixes,
        "max_count": settings.msg_max_codes,
    }


def _format_result(
    passed: list[str],
    rejected: list[str],
    action: str,
    existing: list[str] | None = None,
) -> str:
    """把订阅/取消的结果拼成回复文本。"""
    lines: list[str] = []
    if passed:
        lines.append(f"✅ 已{action} {len(passed)} 个：{', '.join(passed)}")
    if existing:
        lines.append(f"🎬 媒体库已有 {len(existing)} 个：{', '.join(existing)}")
    if rejected:
        lines.append(f"🚫 已过滤 {len(rejected)} 个：{', '.join(rejected)}")
    return "\n".join(lines)


def _split_existing(codes: list[str]) -> tuple[list[str], list[str]]:
    """按媒体库是否已有拆分番号。

    ENABLE_AUTO_COMPLETE 关闭时不查，保持"用户要什么就订什么"。
    媒体库不可达时按不存在处理，宁可多订也别漏订。
    """
    from app import services

    settings = get_settings()
    if not settings.enable_auto_complete or not codes:
        return codes, []

    wanted, existing = [], []
    for code in codes:
        try:
            (existing if services.is_exist_server(code) else wanted).append(code)
        except Exception as exc:
            logger.warning(f"[{code}] 查询媒体库失败，按未入库处理: {exc}")
            wanted.append(code)
    return wanted, existing


def _subscribe_batch(codes: list[str], rejected: list[str]) -> str:
    """订阅一批番号：先过媒体库，再按配置决定是否立即检索。"""
    from app import services

    wanted, existing = _split_existing(codes)
    for code in wanted:
        services.subscribe_code(code)

    reply = _format_result(wanted, rejected, "订阅", existing)

    if wanted and get_settings().msg_auto_download:
        services.download_codes_async(wanted)
        reply += "\n🔎 正在后台检索资源…"
    return reply


@router.get("/message")
def get_message(
    msg_signature: str = "", timestamp: str = "", nonce: str = "", echostr: str = ""
):
    """企业微信回调 URL 验证。"""
    settings = get_settings()
    if not (settings.wechat_token and settings.wechat_encoding_aes_key):
        return Response(content="not configured", status_code=400)

    try:
        from app.modules.notify.WXBizMsgCrypt3 import WXBizMsgCrypt
        crypt = WXBizMsgCrypt(
            settings.wechat_token,
            settings.wechat_encoding_aes_key,
            settings.wechat_corp_id,
        )
        ret, reply = crypt.VerifyURL(msg_signature, timestamp, nonce, echostr)
        if ret != 0:
            logger.warning(f"企业微信回调验证失败: {ret}")
            return Response(content="verify failed", status_code=403)
        return Response(content=reply)
    except Exception as exc:
        logger.error(f"企业微信回调异常: {exc}")
        return Response(content="error", status_code=500)


@router.post("/message")
async def post_message(request: Request):
    """接收上行消息。Telegram 发 JSON，企业微信发加密 XML。"""
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        return await _handle_telegram(request)
    return await _handle_wechat(request)


# ----------------------------------------------------------------------
def handle_telegram_update(payload: dict) -> None:
    """处理一条 Telegram 更新。webhook 与 polling 共用。"""
    settings = get_settings()

    message = payload.get("message") or payload.get("edited_message") or {}
    text = (message.get("text") or "").strip()
    chat_id = str((message.get("chat") or {}).get("id", ""))
    message_id = message.get("message_id", 0)

    if not text:
        return

    # 白名单校验，避免陌生人操作
    whitelist = [x.strip() for x in (settings.telegram_whitelist or "").split("|") if x.strip()]
    if whitelist and chat_id not in whitelist:
        logger.warning(f"拒绝非白名单用户 {chat_id}")
        return

    logger.info(f"[TG] 收到 {chat_id} 的消息: {text[:80]}")
    reply = _dispatch_command(text)
    if reply:
        from app import services
        services.reply_text_msg(reply, message_id, chat_id)
    else:
        logger.info(f"[TG] 消息中未识别到番号，不回复: {text[:80]}")


async def _handle_telegram(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return ResponseEntity.ok()

    handle_telegram_update(payload)
    return ResponseEntity.ok()


async def _handle_wechat(request: Request):
    settings = get_settings()
    if not (settings.wechat_token and settings.wechat_encoding_aes_key):
        return Response(content="")

    params = request.query_params
    body = (await request.body()).decode(errors="replace")

    try:
        from app.modules.notify.WXBizMsgCrypt3 import WXBizMsgCrypt
        crypt = WXBizMsgCrypt(
            settings.wechat_token,
            settings.wechat_encoding_aes_key,
            settings.wechat_corp_id,
        )
        ret, xml_content = crypt.DecryptMsg(
            body,
            params.get("msg_signature", ""),
            params.get("timestamp", ""),
            params.get("nonce", ""),
        )
        if ret != 0:
            return Response(content="")

        import xmltodict
        data = xmltodict.parse(xml_content).get("xml", {})
        text = (data.get("Content") or "").strip()
    except Exception as exc:
        logger.error(f"解析企业微信消息失败: {exc}")
        return Response(content="")

    reply = _dispatch_command(text)
    if reply:
        from app.modules.notify.wechat import WeChatNotifier
        WeChatNotifier().send_text_message(reply)
    return Response(content="")


# ----------------------------------------------------------------------
def _dispatch_command(text: str) -> str:
    """解析指令并返回回复文本。"""
    from app import services

    text = text.strip()
    parts = text.split(maxsplit=1)
    command = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command in ("/help", "/start", "help"):
        return HELP_TEXT

    if command == "/status":
        stats = services.dashboard_stats()
        return (
            f"📊 统计\n"
            f"总番号: {stats['total']}\n"
            f"已订阅: {stats['subscribed']}\n"
            f"下载中: {stats['downloading']}\n"
            f"已完成: {stats['downloaded']}\n"
            f"演员: {stats['actors']}"
        )

    if command == "/sub":
        passed, rejected = normalize_explicit_codes(argument, **_filter_options())
        if not passed and not rejected:
            return "用法: /sub 番号"
        return _subscribe_batch(passed, rejected)

    if command == "/cancel":
        passed, rejected = normalize_explicit_codes(argument, **_filter_options())
        if not passed and not rejected:
            return "用法: /cancel 番号"
        removed = [code for code in passed if services.cancel_subscribe(code)]
        missing = [code for code in passed if code not in removed]
        reply = _format_result(removed, rejected, "取消订阅")
        if missing:
            reply = f"{reply}\n" if reply else ""
            reply += f"ℹ️ 不在订阅列表：{', '.join(missing)}"
        return reply

    if command == "/search":
        code = get_true_code(argument)
        if not code:
            return "用法: /search 番号"
        torrents = services.search_torrents(code)
        if not torrents:
            return f"{code} 未搜到资源"
        lines = [f"🔍 {code} 找到 {len(torrents)} 个资源，前 5 条："]
        for torrent in torrents[:5]:
            lines.append(
                f"• [{torrent.site}] {torrent.title[:40]} "
                f"{torrent.size_mb / 1024:.1f}GB ↑{torrent.seeders}"
            )
        return "\n".join(lines)

    # 不带指令时，识别到番号就当订阅。一条消息里列一串番号是常见写法，
    # 全部取出后按过滤规则筛一遍再订阅。
    passed, rejected = extract_subscribable_codes(text, **_filter_options())
    if not passed and not rejected:
        return ""

    return _subscribe_batch(passed, rejected)
