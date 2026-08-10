"""JWT 签发与校验。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from loguru import logger

from app.core.config import get_settings

ALGORITHM = "HS256"
DEFAULT_EXPIRE_DAYS = 7


def generate_token(username: str, expire_days: int = DEFAULT_EXPIRE_DAYS) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + timedelta(days=expire_days),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def parse_token(token: str) -> dict | None:
    """解析并校验 token，无效返回 None。"""
    if not token:
        return None

    settings = get_settings()
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        logger.debug("token 已过期")
    except jwt.InvalidTokenError as exc:
        logger.debug(f"token 无效: {exc}")
    return None


def valid_token(token: str) -> bool:
    return parse_token(token) is not None


def get_username(token: str) -> str:
    payload = parse_token(token)
    return (payload or {}).get("sub", "")
