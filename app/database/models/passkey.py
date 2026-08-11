"""Passkey（WebAuthn）凭证。

一个账号可以注册多把钥匙（手机、电脑、硬件密钥各一把），
所以单独一张表而不是挂在 user 上。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class Passkey(DBBase):
    __tablename__ = "passkey"

    # 认证器给的凭证 ID，base64url 编码
    credential_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # COSE 格式公钥，base64url 编码
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    # 防重放的签名计数器，每次认证后递增
    sign_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 用户给这把钥匙起的名字，便于在列表里区分
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_used: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "credential_id": self.credential_id,
            "label": self.label or "未命名",
            "create_time": self.create_time,
            "last_used": self.last_used,
        }
