import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Artifact, Export, Job
from app.storage.local import Storage

router = APIRouter()


class ExportOut(BaseModel):
    export_id: uuid.UUID
    status: str
    job_id: uuid.UUID | None
    duration_seconds: float | None
    width: int | None
    height: int | None
    video_codec: str | None
    download_url: str | None


async def _get_export(session: AsyncSession, export_id: uuid.UUID) -> Export:
    export = await session.get(Export, export_id)
    if export is None:
        raise HTTPException(404, "export not found")
    return export


@router.get("/exports/{export_id}")
async def get_export(export_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> ExportOut:
    export = await _get_export(session, export_id)
    ready = export.artifact_id is not None
    job = await session.get(Job, export.job_id) if export.job_id else None
    return ExportOut(
        export_id=export.id,
        status="READY" if ready else (job.status if job else export.status),
        job_id=export.job_id,
        duration_seconds=export.duration_seconds,
        width=export.width,
        height=export.height,
        video_codec=export.video_codec,
        download_url=f"/exports/{export.id}/file" if ready else None,
    )


@router.get("/exports/{export_id}/file")
async def get_export_file(
    export_id: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> FileResponse:
    export = await _get_export(session, export_id)
    artifact = await session.get(Artifact, export.artifact_id) if export.artifact_id else None
    if artifact is None:
        raise HTTPException(409, "export is not ready")
    storage: Storage = request.app.state.storage
    # FileResponse answers Range requests, which browsers need to seek within a <video>.
    return FileResponse(
        storage.path(artifact.storage_key),
        media_type="video/mp4",
        filename=f"mediaforge-{export.id}.mp4",
        content_disposition_type="inline",
    )
