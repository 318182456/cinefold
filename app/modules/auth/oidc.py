"""OIDC 单点登录。

标准授权码流程：跳转到提供商 → 回调带 code → 换 token → 取 userinfo。
只依赖 httpx 与 PyJWT，不引入额外的 OAuth 框架。
"""
from __future__ import annotations

import secrets
import threading
import time
from urllib.parse import urlencode

import httpx
from loguru import logger

from app.core.config import get_settings

# 提供商的端点配置很少变，缓存一段时间省掉每次登录的一次请求
_DISCOVERY_TTL = 3600
_discovery_cache: dict[str, tuple[float, dict]] = {}
_discovery_lock = threading.Lock()

# 待回调的 state。授权跳转与回调之间要带上下文，且防 CSRF
_STATE_TTL = 600
_states: dict[str, dict] = {}
_state_lock = threading.Lock()


class OIDCError(RuntimeError):
    """OIDC 流程中的可预期错误，消息直接展示给用户。"""


def is_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.oidc_enabled
        and settings.oidc_issuer
        and settings.oidc_client_id
        and settings.oidc_client_secret
    )


def public_info() -> dict:
    """给登录页用的信息，不含任何密钥。"""
    settings = get_settings()
    return {
        "enabled": is_configured(),
        "display_name": settings.oidc_display_name or "SSO",
    }


# ----------------------------------------------------------------------
def discover() -> dict:
    """拉取提供商的端点配置。"""
    settings = get_settings()
    issuer = settings.oidc_issuer.rstrip("/")
    if not issuer:
        raise OIDCError("未配置 OIDC 提供商地址")

    with _discovery_lock:
        cached = _discovery_cache.get(issuer)
        if cached and time.time() - cached[0] < _DISCOVERY_TTL:
            return cached[1]

    url = f"{issuer}/.well-known/openid-configuration"
    try:
        with httpx.Client(timeout=15, proxy=settings.proxy or None) as client:
            response = client.get(url)
            response.raise_for_status()
            config = response.json()
    except Exception as exc:
        raise OIDCError(f"读取 OIDC 配置失败: {exc}") from exc

    if not config.get("authorization_endpoint") or not config.get("token_endpoint"):
        raise OIDCError("OIDC 配置缺少必要的端点")

    with _discovery_lock:
        _discovery_cache[issuer] = (time.time(), config)
    return config


def reset_discovery_cache() -> None:
    """改了 issuer 后清缓存，立即用新配置。"""
    with _discovery_lock:
        _discovery_cache.clear()


# ----------------------------------------------------------------------
def _cleanup_states() -> None:
    """顺手清掉过期的 state，避免字典无限增长。"""
    now = time.time()
    expired = [k for k, v in _states.items() if now - v["created"] > _STATE_TTL]
    for key in expired:
        _states.pop(key, None)


def build_authorize_url(redirect_uri: str, next_path: str = "/") -> str:
    """生成授权跳转地址。"""
    settings = get_settings()
    config = discover()

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(16)
    with _state_lock:
        _cleanup_states()
        _states[state] = {
            "created": time.time(),
            "nonce": nonce,
            "redirect_uri": redirect_uri,
            "next": next_path,
        }

    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": settings.oidc_scope or "openid profile email",
        "state": state,
        "nonce": nonce,
    }
    return f"{config['authorization_endpoint']}?{urlencode(params)}"


def pop_state(state: str) -> dict:
    """取出并作废一个 state。找不到说明伪造或已过期。"""
    with _state_lock:
        data = _states.pop(state, None)
    if data is None:
        raise OIDCError("登录状态已失效，请重新发起登录")
    if time.time() - data["created"] > _STATE_TTL:
        raise OIDCError("登录状态已过期，请重新发起登录")
    return data


# ----------------------------------------------------------------------
def exchange_code(code: str, redirect_uri: str) -> dict:
    """用授权码换 token。"""
    settings = get_settings()
    config = discover()

    try:
        with httpx.Client(timeout=20, proxy=settings.proxy or None) as client:
            response = client.post(
                config["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": settings.oidc_client_id,
                    "client_secret": settings.oidc_client_secret,
                },
                headers={"Accept": "application/json"},
            )
    except Exception as exc:
        raise OIDCError(f"换取 token 失败: {exc}") from exc

    if response.status_code >= 400:
        detail = response.text[:200]
        raise OIDCError(f"提供商拒绝了授权码({response.status_code}): {detail}")

    payload = response.json()
    if not payload.get("access_token") and not payload.get("id_token"):
        raise OIDCError("提供商没有返回 token")
    return payload


def fetch_userinfo(tokens: dict) -> dict:
    """取用户信息。优先 userinfo 端点，没有就解 id_token。"""
    settings = get_settings()
    config = discover()

    access_token = tokens.get("access_token")
    endpoint = config.get("userinfo_endpoint")
    if access_token and endpoint:
        try:
            with httpx.Client(timeout=15, proxy=settings.proxy or None) as client:
                response = client.get(
                    endpoint, headers={"Authorization": f"Bearer {access_token}"}
                )
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            logger.warning(f"读取 userinfo 失败，改用 id_token: {exc}")

    id_token = tokens.get("id_token")
    if not id_token:
        raise OIDCError("无法获取用户信息")

    try:
        import jwt
        # 签名已由 token 端点的 TLS 与 client_secret 间接保证，
        # 这里只取 claim，不做二次验签
        return jwt.decode(id_token, options={"verify_signature": False})
    except Exception as exc:
        raise OIDCError(f"解析 id_token 失败: {exc}") from exc


def resolve_username(userinfo: dict) -> str:
    """按 claim 映射决定登录到哪个本地账号。"""
    settings = get_settings()

    # 绑定到固定账号：单用户部署最常见的用法
    if settings.oidc_bind_username:
        return settings.oidc_bind_username

    claim = settings.oidc_username_claim or "preferred_username"
    for key in (claim, "preferred_username", "email", "sub"):
        value = str(userinfo.get(key) or "").strip()
        if value:
            return value
    raise OIDCError("提供商返回的信息里没有可用的用户名")
