"""源目录与视频清单快照，列表查询不访问文件系统。"""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class ScrapeSourceRoot(DBBase):
    __tablename__ = "scrape_source_root"
    root_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    path: Mapped[str] = mapped_column(Text)
    resolved: Mapped[str] = mapped_column(Text, default="")
    available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    last_scan: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ScrapeSourceVideo(DBBase):
    __tablename__ = "scrape_source_video"
    source_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    path: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    detected_code: Mapped[str] = mapped_column(String(64), default="", index=True)
    part: Mapped[int] = mapped_column(Integer, default=0)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    mtime: Mapped[float] = mapped_column(Float, default=0, index=True)
    artifact_status: Mapped[str] = mapped_column(String(24), default="pending")
    links: Mapped[list] = mapped_column(JSON, default=list)
    indexed_at: Mapped[datetime] = mapped_column(DateTime)


class ScrapeSourceMembership(DBBase):
    __tablename__ = "scrape_source_membership"
    root_key: Mapped[str] = mapped_column(ForeignKey("scrape_source_root.root_key"), primary_key=True)
    source_key: Mapped[str] = mapped_column(ForeignKey("scrape_source_video.source_key"), primary_key=True, index=True)
