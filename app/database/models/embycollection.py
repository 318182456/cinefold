"""Only collections explicitly created by Cinefold are managed here."""
from datetime import datetime

from sqlalchemy import DateTime, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class EmbyCollectionLink(DBBase):
    __tablename__ = 'emby_collection_link'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    server: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(128))
    remote_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_fingerprint: Mapped[str] = mapped_column(String(64), default='')
    member_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default='planned')
    error: Mapped[str] = mapped_column(Text, default='')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
