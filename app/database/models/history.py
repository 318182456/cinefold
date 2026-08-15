"""下载历史。用于去重，避免同一番号重复推送下载器。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class History(DBBase):
    __tablename__ = "history"

    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    save_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 种子来自哪个站。下完后要按来源决定是否限速 —— PT 站要保分享率不能限，
    # BT 源下完就没必要继续大量上传。老数据为空，一律按「不是 BT」处理
    site: Mapped[str | None] = mapped_column(String(64), nullable=True)

    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
