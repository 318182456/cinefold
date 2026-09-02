"""演员表。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, String, Text, DateTime
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
    # 是否为用户主动订阅。爬虫库导入的演员只是资料缓存，供搜索和头像用，
    # 不该被演员订阅任务拿去刷新作品，所以这一列为假
    subscribed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )

    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # 演员列表默认按它倒序
    update_time: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, index=True
    )
