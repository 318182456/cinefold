"""已合并的旧媒体路径，阻止延迟删除回调按文件名误匹配保留项。"""
from datetime import datetime

from sqlalchemy import DateTime, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class MediaMerge(DBBase):
    __tablename__ = 'media_merge'
    old_path: Mapped[str] = mapped_column(String(500), primary_key=True)
    keep_path: Mapped[str] = mapped_column(String(500))
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_signature: Mapped[list | None] = mapped_column(JSON, nullable=True)
    keep_signature: Mapped[list | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
