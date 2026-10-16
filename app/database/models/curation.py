"""订阅方案与收藏管理。资料、订阅和观看状态分别保存。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class DownloadProfile(DBBase):
    __tablename__ = "download_profile"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    sort: Mapped[str] = mapped_column(String(255), default="")


class SubscriptionRule(DBBase):
    __tablename__ = "subscription_rule"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    conditions: Mapped[dict] = mapped_column(JSON, default=dict)
    profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class CodeProfile(DBBase):
    __tablename__ = "code_profile"
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile_id: Mapped[int] = mapped_column(Integer)


class RuleExclusion(DBBase):
    __tablename__ = "rule_exclusion"
    code: Mapped[str] = mapped_column(String(64), primary_key=True)


class LibraryPreference(DBBase):
    __tablename__ = "library_preference"
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    watch_later: Mapped[bool] = mapped_column(Boolean, default=False)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class Collection(DBBase):
    __tablename__ = "collection"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16), default="manual")
    conditions: Mapped[dict] = mapped_column(JSON, default=dict)


class CollectionItem(DBBase):
    __tablename__ = "collection_item"
    collection_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), primary_key=True)


class EmbyWatch(DBBase):
    __tablename__ = "emby_watch"
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    played: Mapped[bool] = mapped_column(Boolean, default=False)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    server: Mapped[str] = mapped_column(String(64), default="")
    play_count: Mapped[int] = mapped_column(Integer, default=0)
    position_ticks: Mapped[int] = mapped_column(BigInteger, default=0)
    runtime_ticks: Mapped[int] = mapped_column(BigInteger, default=0)
    last_played: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sync_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
