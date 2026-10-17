"""Explicit duplicate choices; unlike MediaMerge these do not prove equal content."""
from datetime import datetime

from sqlalchemy import DateTime, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class ScrapeDuplicateDecision(DBBase):
    __tablename__ = 'scrape_duplicate_decision'
    source_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_path: Mapped[str] = mapped_column(Text)
    target_path: Mapped[str] = mapped_column(Text)
    source_signature: Mapped[list] = mapped_column(JSON)
    target_signature: Mapped[list] = mapped_column(JSON)
    policy: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
