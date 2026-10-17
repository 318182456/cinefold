"""Persistent task summaries and paginated film results."""
from sqlalchemy import Boolean, Float, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.database.base import DBBase


class ScrapeTask(DBBase):
    __tablename__ = 'scrape_task'
    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    phase: Mapped[str] = mapped_column(String(32), default='running')
    parent_id: Mapped[str] = mapped_column(String(128), default='')
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default='')
    quarantine_dir: Mapped[str] = mapped_column(Text, default='')
    started_at: Mapped[float] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    indexed: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)


class ScrapeTaskResult(DBBase):
    __tablename__ = 'scrape_task_result'
    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    code: Mapped[str] = mapped_column(String(128), primary_key=True)
    ok: Mapped[bool] = mapped_column(Boolean)
    data: Mapped[dict] = mapped_column(JSON)
