"""Persistent downloader metadata and exact file index; removed torrents retain history."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.database.base import DBBase


class TorrentManifest(DBBase):
    __tablename__ = 'torrent_manifest'
    connection: Mapped[str] = mapped_column(String(64), primary_key=True)
    hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    client: Mapped[str] = mapped_column(String(32), index=True)
    present: Mapped[bool] = mapped_column(Boolean, default=True)
    signature: Mapped[str] = mapped_column(String(64), default='')
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    seen_at: Mapped[datetime] = mapped_column(DateTime)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(200), default='')


class TorrentFile(DBBase):
    __tablename__ = 'torrent_file'
    __table_args__ = (Index('ix_torrent_file_match', 'connection', 'match_key'),)
    connection: Mapped[str] = mapped_column(String(64), primary_key=True)
    hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    path_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    path: Mapped[str] = mapped_column(Text)
    match_key: Mapped[str] = mapped_column(String(64))
