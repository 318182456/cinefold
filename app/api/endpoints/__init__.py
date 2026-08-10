"""API 鉴权依赖。"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select

from app.database.models import User
from app.database.session import session_scope
from app.utils.jwtutil import generate_token, parse_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/login", auto_error=False)
optional_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/login", auto_error=False)


def create_jwt_token(username: str) -> str:
    return generate_token(username)


def _token_from_request(request: Request, bearer: str | None) -> str:
    """依次尝试 Bearer 头、query 参数、自定义头。

    图片代理等场景无法带 Authorization 头，因此额外支持 query token。
    """
    if bearer:
        return bearer
    query_token = request.query_params.get("token")
    if query_token:
        return query_token
    return request.headers.get("X-Token", "")


def get_current_user(
    request: Request, bearer: str | None = Depends(oauth2_scheme)
) -> str:
    """校验 JWT 或长期 token，返回用户名。"""
    token = _token_from_request(request, bearer)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供凭证",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = parse_token(token)
    if payload is not None:
        return payload.get("sub", "")

    # 退回长期 token（供 Telegram bot 等外部调用）
    with session_scope() as session:
        user = session.scalar(select(User).where(User.token == token))
        if user is not None:
            return user.username

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="凭证无效或已过期",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_optional_user(
    request: Request, bearer: str | None = Depends(optional_oauth2_scheme)
) -> str:
    """可选鉴权，未登录返回空串。"""
    try:
        return get_current_user(request, bearer)
    except HTTPException:
        return ""
