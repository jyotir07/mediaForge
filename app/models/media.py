from sqlalchemy import BigInteger, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UUIDPk


class Media(UUIDPk, CreatedAt, Base):
    __tablename__ = "media"

    original_filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    video_codec: Mapped[str | None] = mapped_column(String(64))
    audio_codec: Mapped[str | None] = mapped_column(String(64))
    container: Mapped[str | None] = mapped_column(String(64))
    bitrate: Mapped[int | None] = mapped_column(BigInteger)
    rotation: Mapped[int | None] = mapped_column(Integer)
    probe_status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
