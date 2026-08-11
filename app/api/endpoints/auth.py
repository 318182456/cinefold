"""OIDC 单点登录与 Passkey 登录。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from loguru import logger
from pydantic import BaseModel

from app.api.endpoints import create_jwt_token, get_current_user
from app.core.config import get_settings
from app.database.models import User
from app.database.session import session_scope
from app.schemas.reponse import ResponseEntity

router = APIRouter(tags=["auth"])

OIDC_CALLBACK_PATH = "/api/v1/auth/oidc/callback"


def _origin(request: Request) -> str:
    """站点根地址。优先用配置里的外网地址，避免反代改写导致对不上。

    WebAuthn 校验 origin、OIDC 校验 redirect_uri，两者都必须与浏览器
    实际访问的地址完全一致。
    """
    external = (get_settings().external_domain or "").strip().rstrip("/")
    if external:
        if not external.startswith(("http://", "https://")):
            external = f"https://{external}"
        return external

    # 反代会把原始协议放在 X-Forwarded-Proto，直接用 request.url 会拿到 http
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return f"{proto}://{host}"


# ----------------------------------------------------------------------
# 登录页要展示哪些登录方式
# ----------------------------------------------------------------------
@router.get("/auth/methods")
def auth_methods(request: Request):
    """公开接口，未登录时也要能读。"""
    from app.modules.auth import oidc, passkey

    try:
        has_passkey = passkey.has_credentials()
    except Exception:
        has_passkey = False

    return ResponseEntity.ok({
        "password": True,
        "oidc": oidc.public_info(),
        "passkey": {"enabled": has_passkey},
    })


# ----------------------------------------------------------------------
# OIDC
# ----------------------------------------------------------------------
@router.get("/auth/oidc/login")
def oidc_login(request: Request, next: str = "/"):
    """跳转到提供商的授权页。"""
    from app.modules.auth import oidc

    if not oidc.is_configured():
        return ResponseEntity.fail("未配置 OIDC 单点登录", code=400)

    try:
        url = oidc.build_authorize_url(
            redirect_uri=f"{_origin(request)}{OIDC_CALLBACK_PATH}",
            next_path=next,
        )
    except oidc.OIDCError as exc:
        return ResponseEntity.fail(str(exc), code=502)

    return RedirectResponse(url, status_code=302)


@router.get("/auth/oidc/callback")
def oidc_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
):
    """提供商回调。成功后带 token 跳回前端。"""
    from app.modules.auth import oidc

    origin = _origin(request)

    def fail(message: str):
        # 回调是浏览器直接访问的，返回 JSON 用户看不懂，跳回登录页提示
        from urllib.parse import quote
        return RedirectResponse(
            f"{origin}/login?sso_error={quote(message[:200])}", status_code=302
        )

    if error:
        return fail(f"提供商返回错误: {error}")
    if not code or not state:
        return fail("回调缺少必要参数")

    try:
        stored = oidc.pop_state(state)
        tokens = oidc.exchange_code(code, stored["redirect_uri"])
        userinfo = oidc.fetch_userinfo(tokens)
        username = oidc.resolve_username(userinfo)
    except oidc.OIDCError as exc:
        logger.warning(f"OIDC 登录失败: {exc}")
        return fail(str(exc))
    except Exception as exc:
        logger.exception(f"OIDC 登录异常: {exc}")
        return fail("单点登录失败，请查看日志")

    # 本地没有这个账号就建一个。密码留空表示不能用密码登录
    with session_scope() as session:
        user = session.get(User, username)
        if user is None:
            session.add(User(username=username, password=""))
            logger.info(f"OIDC 首次登录，创建账号 {username}")

    token = create_jwt_token(username)
    next_path = stored.get("next") or "/"
    if not next_path.startswith("/"):
        next_path = "/"

    from urllib.parse import quote
    return RedirectResponse(
        f"{origin}/login?sso_token={token}&sso_user={quote(username)}"
        f"&next={quote(next_path)}",
        status_code=302,
    )


class OIDCTestRequest(BaseModel):
    issuer: str = ""


@router.post("/auth/oidc/test")
def oidc_test(
    body: OIDCTestRequest, current_user: str = Depends(get_current_user)
):
    """探测提供商的端点配置，确认地址填对了。"""
    from app.modules.auth import oidc

    settings = get_settings()
    issuer = (body.issuer or settings.oidc_issuer or "").strip().rstrip("/")
    if not issuer:
        return ResponseEntity.ok({"success": False, "message": "请先填写提供商地址"})

    # 用页面上的当前值探测，省得先保存
    original = settings.oidc_issuer
    settings.oidc_issuer = issuer
    oidc.reset_discovery_cache()
    try:
        config = oidc.discover()
        return ResponseEntity.ok({
            "success": True,
            "message": "连接成功",
            "authorization_endpoint": config.get("authorization_endpoint", ""),
            "token_endpoint": config.get("token_endpoint", ""),
            "userinfo_endpoint": config.get("userinfo_endpoint", ""),
        })
    except oidc.OIDCError as exc:
        return ResponseEntity.ok({"success": False, "message": str(exc)})
    finally:
        settings.oidc_issuer = original
        oidc.reset_discovery_cache()


@router.get("/auth/oidc/redirect-uri")
def oidc_redirect_uri(request: Request, current_user: str = Depends(get_current_user)):
    """回调地址，填到提供商那边的白名单里。"""
    return ResponseEntity.ok({"redirect_uri": f"{_origin(request)}{OIDC_CALLBACK_PATH}"})


# ----------------------------------------------------------------------
# Passkey
# ----------------------------------------------------------------------
class PasskeyFinishRequest(BaseModel):
    credential: dict
    label: str = ""


@router.post("/auth/passkey/register/begin")
def passkey_register_begin(
    request: Request, current_user: str = Depends(get_current_user)
):
    """开始注册。必须已登录——新增钥匙是敏感操作。"""
    from app.modules.auth import passkey

    try:
        options = passkey.start_registration(current_user, _origin(request))
    except passkey.PasskeyError as exc:
        return ResponseEntity.fail(str(exc), code=400)
    return ResponseEntity.ok({"options": json.loads(options)})


@router.post("/auth/passkey/register/finish")
def passkey_register_finish(
    body: PasskeyFinishRequest,
    request: Request,
    current_user: str = Depends(get_current_user),
):
    from app.modules.auth import passkey

    try:
        result = passkey.finish_registration(
            current_user, body.credential, _origin(request), body.label
        )
    except passkey.PasskeyError as exc:
        return ResponseEntity.fail(str(exc), code=400)
    return ResponseEntity.ok(result, message="Passkey 已添加")


class PasskeyAuthBeginRequest(BaseModel):
    username: str = ""


@router.post("/auth/passkey/login/begin")
def passkey_login_begin(body: PasskeyAuthBeginRequest, request: Request):
    """开始认证。公开接口。"""
    from app.modules.auth import passkey

    try:
        options = passkey.start_authentication(_origin(request), body.username)
    except passkey.PasskeyError as exc:
        return ResponseEntity.fail(str(exc), code=400)
    return ResponseEntity.ok({"options": json.loads(options)})


@router.post("/auth/passkey/login/finish")
def passkey_login_finish(body: PasskeyFinishRequest, request: Request):
    """校验通过后签发 JWT。"""
    from app.modules.auth import passkey

    try:
        username = passkey.finish_authentication(body.credential, _origin(request))
    except passkey.PasskeyError as exc:
        return ResponseEntity.fail(str(exc), code=401)

    return ResponseEntity.ok({
        "token": create_jwt_token(username),
        "username": username,
    })


@router.get("/auth/passkey/list")
def passkey_list(current_user: str = Depends(get_current_user)):
    from app.modules.auth import passkey
    return ResponseEntity.ok({"items": passkey.list_credentials(current_user)})


@router.delete("/auth/passkey/{credential_id:path}")
def passkey_delete(
    credential_id: str, current_user: str = Depends(get_current_user)
):
    from app.modules.auth import passkey

    if not passkey.delete_credential(current_user, credential_id):
        return ResponseEntity.fail("凭证不存在", code=404)
    return ResponseEntity.ok(message="已删除")
