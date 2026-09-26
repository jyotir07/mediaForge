import uuid
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator


class TrimOp(BaseModel):
    type: Literal["trim"] = "trim"
    start: float = Field(ge=0)
    end: float

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class EDL(BaseModel):
    """Edit Decision List: the only input the export worker accepts. Operations are chronological and
    non-overlapping, so executing them in order yields the edit."""

    version: Literal[1] = 1
    source_media_id: uuid.UUID
    operations: list[TrimOp] = Field(min_length=1)

    @model_validator(mode="after")
    def _chronological(self) -> Self:
        for a, b in zip(self.operations, self.operations[1:], strict=False):
            if b.start < a.end:
                raise ValueError("operations must be chronological and non-overlapping")
        return self

    @property
    def duration(self) -> float:
        return sum(op.end - op.start for op in self.operations)
