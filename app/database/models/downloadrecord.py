"""下载器任务的持久化快照；种子移除后仍可查看下载历史。"""
from datetime import datetime
from sqlalchemy import DateTime, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from app.database.base import DBBase


class DownloadRecord(DBBase):
    __tablename__ = 'download_record'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    client: Mapped[str] = mapped_column(String(32), index=True)
    hash: Mapped[str] = mapped_column(String(64), index=True)
    code: Mapped[str] = mapped_column(String(64), default='', index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
