import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MediaUploadResponse(BaseModel):
    media_id: uuid.UUID
    probe_job_id: uuid.UUID | None


class ArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    storage_key: str
    mime_type: str
    size_bytes: int
    created_at: datetime


class JobSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    status: str
    stage: str | None
    progress: float


class MediaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    size_bytes: int
    probe_status: str
    created_at: datetime
    artifacts: list[ArtifactOut]
    jobs: list[JobSummary]


class MediaMetadataOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    duration_seconds: float
    width: int
    height: int
    fps: float
    rotation: int | None
    video_codec: str
    audio_codec: str | None
    container: str
    bitrate: int | None
    size_bytes: int


class EditRequest(BaseModel):
    request: str = Field(min_length=1, max_length=500)

    @field_validator("request")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("request must not be blank")
        return v

    def normalized(self) -> str:
        # Equivalent phrasings ("  Make a REEL") share one idempotency key and therefore one job.
        return " ".join(self.request.split()).lower()
