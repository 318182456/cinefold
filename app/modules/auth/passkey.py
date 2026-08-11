"""Passkey（WebAuthn）注册与认证。

签名验证交给 webauthn 库，这里只管挑战的生成、暂存与凭证读写。
浏览器要求 WebAuthn 必须跑在 HTTPS 上（localhost 除外）。
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import select
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.core.config import get_settings
from app.database.models import Passkey
from app.database.session import session_scope

# 挑战有效期。用户要有时间碰指纹或插密钥
_CHALLENGE_TTL = 300
_challenges: dict[str, dict] = {}
_lock = threading.Lock()


class PasskeyError(RuntimeError):
    """流程中的可预期错误，消息直接展示给用户。"""


def _rp_id(origin: str) -> str:
    """Relying Party ID 必须是站点域名，不含端口与协议。"""
    configured = (get_settings().webauthn_rp_id or "").strip()
    if configured:
        return configured

    host = urlparse(origin).hostname or ""
    if not host:
        raise PasskeyError("无法确定站点域名，请在设置里填写 WebAuthn RP ID")
    return host


def _put_challenge(key: str, challenge: bytes, **extra) -> None:
    with _lock:
        now = time.time()
        # 顺手清过期的，避免字典无限增长
        for k in [k for k, v in _challenges.items() if now - v["created"] > _CHALLENGE_TTL]:
            _challenges.pop(k, None)
        _challenges[key] = {"challenge": challenge, "created": now, **extra}


def _pop_challenge(key: str) -> dict:
    with _lock:
        data = _challenges.pop(key, None)
    if data is None:
        raise PasskeyError("挑战已失效，请重新操作")
    if time.time() - data["created"] > _CHALLENGE_TTL:
        raise PasskeyError("挑战已过期，请重新操作")
    return data


# ----------------------------------------------------------------------
def list_credentials(username: str) -> list[dict]:
    with session_scope() as session:
        rows = session.scalars(
            select(Passkey).where(Passkey.username == username)
        ).all()
        return [row.to_dict() for row in rows]


def delete_credential(username: str, credential_id: str) -> bool:
    with session_scope() as session:
        row = session.get(Passkey, credential_id)
        if row is None or row.username != username:
            return False
        session.delete(row)
    return True


def has_credentials() -> bool:
    """有没有任何已注册的 Passkey，决定登录页要不要显示按钮。"""
    with session_scope() as session:
        return session.scalar(select(Passkey.credential_id).limit(1)) is not None


# ----------------------------------------------------------------------
def start_registration(username: str, origin: str) -> str:
    """生成注册选项，返回给浏览器的 JSON。"""
    settings = get_settings()
    rp_id = _rp_id(origin)

    with session_scope() as session:
        existing = session.scalars(
            select(Passkey.credential_id).where(Passkey.username == username)
        ).all()

    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=settings.webauthn_rp_name or "cinefold",
        user_name=username,
        user_display_name=username,
        # 已注册的排除掉，同一把钥匙不必重复注册
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid))
            for cid in existing
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            # 可发现凭证，登录时不必先输用户名
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )

    _put_challenge(f"reg:{username}", options.challenge, rp_id=rp_id)
    return options_to_json(options)


def finish_registration(
    username: str, credential: dict, origin: str, label: str = ""
) -> dict:
    """校验注册结果并存下公钥。"""
    stored = _pop_challenge(f"reg:{username}")

    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=stored["challenge"],
            expected_rp_id=stored["rp_id"],
            expected_origin=origin,
        )
    except Exception as exc:
        raise PasskeyError(f"注册校验失败: {exc}") from exc

    credential_id = bytes_to_base64url(verification.credential_id)
    with session_scope() as session:
        row = session.get(Passkey, credential_id)
        if row is None:
            row = Passkey(credential_id=credential_id, username=username)
            session.add(row)
        row.public_key = bytes_to_base64url(verification.credential_public_key)
        row.sign_count = verification.sign_count or 0
        row.label = (label or "").strip()[:64] or None
        result = row.to_dict()

    logger.info(f"[{username}] 新增 Passkey {credential_id[:12]}…")
    return result


# ----------------------------------------------------------------------
def start_authentication(origin: str, username: str = "") -> str:
    """生成认证选项。username 为空时依赖可发现凭证。"""
    rp_id = _rp_id(origin)

    allow: list[PublicKeyCredentialDescriptor] = []
    if username:
        with session_scope() as session:
            ids = session.scalars(
                select(Passkey.credential_id).where(Passkey.username == username)
            ).all()
        allow = [
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid)) for cid in ids
        ]
        if not allow:
            raise PasskeyError("该账号还没有注册 Passkey")

    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=allow or None,
        user_verification=UserVerificationRequirement.PREFERRED,
    )

    # 认证时还不知道是谁，挑战按 rp_id 存
    _put_challenge(f"auth:{rp_id}", options.challenge, rp_id=rp_id)
    return options_to_json(options)


def finish_authentication(credential: dict, origin: str) -> str:
    """校验认证结果，返回登录成功的用户名。"""
    rp_id = _rp_id(origin)
    stored = _pop_challenge(f"auth:{rp_id}")

    credential_id = credential.get("id") or credential.get("rawId") or ""
    if not credential_id:
        raise PasskeyError("凭证格式不正确")

    with session_scope() as session:
        row = session.get(Passkey, credential_id)
        if row is None:
            raise PasskeyError("这把钥匙没有注册过")
        username = row.username
        public_key = row.public_key
        sign_count = row.sign_count

    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=stored["challenge"],
            expected_rp_id=stored["rp_id"],
            expected_origin=origin,
            credential_public_key=base64url_to_bytes(public_key),
            credential_current_sign_count=sign_count,
        )
    except Exception as exc:
        raise PasskeyError(f"认证失败: {exc}") from exc

    from datetime import datetime

    with session_scope() as session:
        row = session.get(Passkey, credential_id)
        if row is not None:
            # 计数器倒退说明凭证被克隆，库已经在上面拦了，这里只更新
            row.sign_count = verification.new_sign_count
            row.last_used = datetime.now()

    logger.info(f"[{username}] 通过 Passkey 登录")
    return username
