"""演员资料缓存；与用户订阅标记分离。"""
from datetime import datetime

from sqlalchemy import DateTime, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class ActorProfile(DBBase):
    __tablename__ = 'actor_profile'
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default='')
