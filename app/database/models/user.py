"""用户表。单用户系统，仅用于 Web UI 登录。"""
from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class User(DBBase):
    __tablename__ = "user"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    # bcrypt 哈希，不存明文
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    # 供 Telegram bot 等外部调用的长期 token
    token: Mapped[str | None] = mapped_column(Text, nullable=True)
