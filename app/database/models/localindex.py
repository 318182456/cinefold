"""On-disk sources indexed without retaining whole libraries in Python."""
from datetime import datetime
from sqlalchemy import String, Text, Integer, JSON, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.database.base import DBBase


class SubtitleDirectory(DBBase):
    __tablename__ = 'subtitle_directory'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    root: Mapped[str] = mapped_column(String(64), index=True)
    path: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[list] = mapped_column(JSON)
    children: Mapped[list] = mapped_column(JSON, default=list)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SubtitleFile(DBBase):
    __tablename__ = 'subtitle_file'
    __table_args__ = (Index('ix_subtitle_identity', 'root', 'code', 'part'),)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    root: Mapped[str] = mapped_column(String(64), index=True)
    directory: Mapped[str] = mapped_column(String(64), index=True)
    path: Mapped[str] = mapped_column(Text)
    code: Mapped[str] = mapped_column(String(64))
    part: Mapped[int] = mapped_column(Integer, default=0)
