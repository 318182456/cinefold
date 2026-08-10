"""下载历史。用于去重，避免同一番号重复推送下载器。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class History(DBBase):
    __tablename__ = "history"

    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    save_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
