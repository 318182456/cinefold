"""SQLAlchemy 声明基类。"""
from __future__ import annotations

from datetime import datetime, date

from sqlalchemy.orm import DeclarativeBase


class DBBase(DeclarativeBase):
    def to_dict(self) -> dict:
        out = {}
        for col in self.__table__.columns:
            value = getattr(self, col.name)
            if isinstance(value, datetime):
                value = value.strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(value, date):
                value = value.strftime("%Y-%m-%d")
            out[col.name] = value
        return out

    def __repr__(self) -> str:
        pk = [c.name for c in self.__table__.primary_key.columns]
        parts = ", ".join(f"{k}={getattr(self, k)!r}" for k in pk)
        return f"<{self.__class__.__name__} {parts}>"

    def __str__(self) -> str:
        return self.__repr__()
