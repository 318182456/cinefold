"""Native crawler resources. Preview URLs are metadata, never downloaded here."""
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, Float, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.database.base import DBBase


class ResourcePost(DBBase):
    __tablename__ = 'resource_post'
    __table_args__ = (Index('ix_resource_source_tid', 'source', 'tid'),)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    tid: Mapped[int] = mapped_column(BigInteger)
    code: Mapped[str] = mapped_column(String(64), default='', index=True)
    title: Mapped[str] = mapped_column(Text)
    section: Mapped[str] = mapped_column(String(64), default='')
    category: Mapped[str] = mapped_column(String(128), default='')
    detail_url: Mapped[str] = mapped_column(Text, default='')
    published: Mapped[str] = mapped_column(String(32), default='')
    size_mb: Mapped[float] = mapped_column(Float, default=0)
    magnets: Mapped[list] = mapped_column(JSON, default=list)
    ed2k: Mapped[list] = mapped_column(JSON, default=list)
    previews: Mapped[list] = mapped_column(JSON, default=list)
    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class CrawlerAttempt(DBBase):
    __tablename__ = 'crawler_attempt'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    tid: Mapped[int] = mapped_column(BigInteger)
    section: Mapped[str] = mapped_column(String(32))
    retry_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    error: Mapped[str] = mapped_column(String(128), default='')
