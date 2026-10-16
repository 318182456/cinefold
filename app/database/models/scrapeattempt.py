"""下载源文件最近一次刮削记录，重启后仍可查看失败原因。"""
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class ScrapeAttempt(DBBase):
    __tablename__ = "scrape_attempt"
    source_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_path: Mapped[str] = mapped_column(Text)
    code: Mapped[str] = mapped_column(String(64), default="")
    job_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(24))
    error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
