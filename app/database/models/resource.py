"""资源采集索引，与影片资料和订阅状态分开。"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class ResourceFeed(DBBase):
    __tablename__ = "resource_feed"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16), default="json")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cookie: Mapped[str] = mapped_column(Text, default="")
    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Resource(DBBase):
    __tablename__ = "resource"
    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    magnet: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(128))
    detail_url: Mapped[str] = mapped_column(Text, default="")
    size_mb: Mapped[float] = mapped_column(Float, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ResourceMatch(DBBase):
    __tablename__ = "resource_match"
    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
