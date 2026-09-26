import uuid
from typing import Any

from pydantic import BaseModel

from app.models import Job


class JobAccepted(BaseModel):
    job_id: uuid.UUID
    status: str


class JobOut(BaseModel):
    job_id: uuid.UUID
    media_id: uuid.UUID
    type: str
    status: str
    stage: str | None
    progress: float
    message: str | None
    attempt: int
    max_attempts: int
    error_code: str | None
    error_message: str | None
    result: dict[str, Any] | None

    @classmethod
    def from_job(cls, job: Job) -> "JobOut":
        return cls(
            job_id=job.id,
            media_id=job.media_id,
            type=job.type,
            status=job.status,
            stage=job.stage,
            progress=job.progress,
            message=job.message,
            attempt=job.attempt,
            max_attempts=job.max_attempts,
            error_code=job.error_code,
            error_message=job.error_message,
            result=job.result,
        )
