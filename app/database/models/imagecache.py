"""Small metadata catalog for bounded cache maintenance and LRU eviction."""
from sqlalchemy import String, Text, BigInteger, Float, Boolean, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.database.base import DBBase


class ImageCacheFile(DBBase):
    __tablename__ = 'image_cache_file'
    __table_args__ = (Index('ix_image_cache_lru', 'protected', 'recoverable', 'accessed'),)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    relative: Mapped[str] = mapped_column(Text)
    code: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(BigInteger)
    accessed: Mapped[float] = mapped_column(Float)
    fingerprint: Mapped[list] = mapped_column(JSON)
    protected: Mapped[bool] = mapped_column(Boolean)
    recoverable: Mapped[bool] = mapped_column(Boolean)
    still: Mapped[bool] = mapped_column(Boolean)
    scanned: Mapped[float] = mapped_column(Float, index=True)
