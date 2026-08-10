"""登录与账号管理。"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from app.api.endpoints import create_jwt_token, get_current_user
from app.database.models import User
from app.database.session import session_scope
from app.database.utils.setup import hash_password, verify_password
from app.schemas.reponse import ResponseEntity

router = APIRouter(tags=["admin"])


class LoginRequest(BaseModel):
    username: str
    password: str


class UpdateUserRequest(BaseModel):
    username: str | None = None
    password: str | None = None


@router.post("/login")
def login(body: LoginRequest):
    with session_scope() as session:
        user = session.get(User, body.username)
        if user is None or not verify_password(body.password, user.password):
            return ResponseEntity.fail("用户名或密码错误", code=401)
        username = user.username

    return ResponseEntity.ok({"token": create_jwt_token(username), "username": username})


@router.get("/user/token")
def init_token(current_user: str = Depends(get_current_user)):
    """获取长期 token，不存在则生成。"""
    with session_scope() as session:
        user = session.get(User, current_user)
        if user is None:
            return ResponseEntity.fail("用户不存在", code=404)
        if not user.token:
            user.token = secrets.token_urlsafe(32)
        return ResponseEntity.ok({"token": user.token})


@router.post("/user/token")
def reset_token(current_user: str = Depends(get_current_user)):
    """重置长期 token。"""
    with session_scope() as session:
        user = session.get(User, current_user)
        if user is None:
            return ResponseEntity.fail("用户不存在", code=404)
        user.token = secrets.token_urlsafe(32)
        return ResponseEntity.ok({"token": user.token})


@router.post("/profile")
def update_user(body: UpdateUserRequest, current_user: str = Depends(get_current_user)):
    """修改用户名或密码。改用户名时需要重新登录。"""
    with session_scope() as session:
        user = session.get(User, current_user)
        if user is None:
            return ResponseEntity.fail("用户不存在", code=404)

        if body.password:
            user.password = hash_password(body.password)

        if body.username and body.username != current_user:
            if session.get(User, body.username) is not None:
                return ResponseEntity.fail("用户名已存在", code=400)
            # 主键无法直接改，需要新建后删除旧行
            session.add(User(
                username=body.username,
                password=user.password,
                token=user.token,
            ))
            session.delete(user)
            return ResponseEntity.ok({"token": create_jwt_token(body.username),
                                      "username": body.username})

    return ResponseEntity.ok(message="修改成功")


@router.get("/profile")
def get_profile(current_user: str = Depends(get_current_user)):
    return ResponseEntity.ok({"username": current_user})
