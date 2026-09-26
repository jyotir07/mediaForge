import uuid
from typing import Any

from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UUIDPk


class EditPlan(UUIDPk, CreatedAt, Base):
    __tablename__ = "edit_plans"

    media_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    request: Mapped[str] = mapped_column(Text)
    candidate_data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    decisions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    accepted_operations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
