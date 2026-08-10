"""通用 KV 缓存。按 namespace 分区，存榜单快照等。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, Integer, DateTime, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class Cache(DBBase):
    __tablename__ = "cache"
    __table_args__ = (UniqueConstraint("namespace", "key", name="uq_cache_ns_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)

    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
