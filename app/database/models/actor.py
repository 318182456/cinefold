"""演员表。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class Actor(DBBase):
    __tablename__ = "actor"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    # 日文名/罗马音等别名，抓取时用于匹配
    name_2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    photo: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 只订阅该日期之后发行的作品，避免把整个作品史都拉下来
    limit_date: Mapped[str | None] = mapped_column(String(32), nullable=True)

    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    update_time: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )
