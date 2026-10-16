"""逐次播放与已观测到的活动时间段。时间使用 UTC，与 Emby 累计状态分开。"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class PlaybackSession(DBBase):
    __tablename__ = "playback_session"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server: Mapped[str] = mapped_column(String(64), index=True)
    session_key: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    user_name: Mapped[str] = mapped_column(String(255), default="")
    item_id: Mapped[str] = mapped_column(String(128), index=True)
    code: Mapped[str] = mapped_column(String(64), default="", index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    device: Mapped[str] = mapped_column(String(255), default="")
    client: Mapped[str] = mapped_column(String(255), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    position_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    watched_seconds: Mapped[float] = mapped_column(Float, default=0)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class PlaybackSpan(DBBase):
    __tablename__ = "playback_span"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    playback_id: Mapped[int] = mapped_column(ForeignKey("playback_session.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime, index=True)
