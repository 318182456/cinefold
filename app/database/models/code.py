"""番号主表。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, Float, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class CodeStatus:
    """number 的订阅状态流转。"""
    NONE = 0          # 未订阅
    SUBSCRIBED = 1    # 已订阅，等待资源
    DOWNLOADING = 2   # 已推送下载器
    DOWNLOADED = 3    # 下载完成
    COMPLETED = 4     # 已入库
    FAILED = 5        # 失败


class Code(DBBase):
    __tablename__ = "code"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    cn_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[int] = mapped_column(Integer, default=CodeStatus.NONE, nullable=False)

    release_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration: Mapped[str | None] = mapped_column(String(32), nullable=True)
    producer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    publisher: Mapped[str | None] = mapped_column(String(128), nullable=True)
    series: Mapped[str | None] = mapped_column(String(255), nullable=True)
    genres: Mapped[str | None] = mapped_column(Text, nullable=True)
    casts: Mapped[str | None] = mapped_column(Text, nullable=True)
    star: Mapped[float | None] = mapped_column(Float, nullable=True)

    banner: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_banner: Mapped[str | None] = mapped_column(Text, nullable=True)
    still_photo: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_still_photo: Mapped[str | None] = mapped_column(Text, nullable=True)
    poster: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    mode: Mapped[str | None] = mapped_column(String(32), nullable=True)

    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    update_time: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )
