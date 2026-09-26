import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.jobs import service
from app.logging import log_event
from app.media.sniff import ALLOWED_EXTENSIONS, MIME_TYPES, SNIFF_BYTES, is_consistent, sniff_container
from app.models import Artifact, Job, Media
from app.queue import redis_queue
from app.schemas.job import JobAccepted
from app.schemas.media import (
    ArtifactOut,
    EditRequest,
    JobSummary,
    MediaMetadataOut,
    MediaOut,
    MediaUploadResponse,
)
from app.storage.local import Storage, TooLarge, media_key

router = APIRouter()


class _Unsupported(Exception):
    pass


def _check_container(head: bytes, ext: str) -> str:
    container = sniff_container(head)
    if container is None or not is_consistent(ext, container):
        raise _Unsupported
    return container


async def _sniffed(chunks: AsyncIterator[bytes], ext: str, found: dict[str, str]) -> AsyncIterator[bytes]:
    """Pass the body through, validating the container from its first bytes before anything is kept."""
    head = b""
    async for chunk in chunks:
        if "container" not in found:
            head += chunk
            if len(head) < SNIFF_BYTES:
                continue
            found["container"] = _check_container(head, ext)
            chunk, head = head, b""
        yield chunk
    if "container" not in found:
        found["container"] = _check_container(head, ext)
        yield head


async def _enqueue_best_effort(request: Request, job: Job) -> None:
    # The job row is already durable; if Redis is down the worker sweeper re-enqueues it later.
    try:
        await redis_queue.enqueue(request.app.state.redis, job.type, job.id)
    except Exception:
        log_event("job.enqueue_failed", level=logging.WARNING, exc_info=True, job_id=job.id)


async def _get_media(session: AsyncSession, media_id: uuid.UUID) -> Media:
    media = await session.get(Media, media_id)
    if media is None:
        raise HTTPException(404, "media not found")
    return media


@router.post("/media", status_code=201)
async def upload_media(
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
    session: AsyncSession = Depends(get_session),
) -> MediaUploadResponse:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, f"unsupported file extension {ext!r}")

    max_bytes: int = request.app.state.settings.max_upload_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise HTTPException(413, "file too large")

    storage: Storage = request.app.state.storage
    media_id = uuid.uuid4()
    key = media_key(media_id, f"source{ext}")
    found: dict[str, str] = {}
    try:
        size = await storage.write_stream(key, _sniffed(request.stream(), ext, found), max_bytes)
    except TooLarge:
        raise HTTPException(413, "file too large") from None
    except _Unsupported:
        raise HTTPException(415, "file content is not a supported video container") from None

    try:
        session.add(Media(id=media_id, original_filename=filename, storage_key=key, size_bytes=size))
        await session.flush()
        session.add(
            Artifact(
                media_id=media_id,
                type="SOURCE",
                storage_key=key,
                mime_type=MIME_TYPES[found["container"]],
                size_bytes=size,
            )
        )
        await session.commit()
    except BaseException:
        storage.delete(key)
        raise

    job, _ = await service.create_or_get(session, media_id, "probe", {})
    await _enqueue_best_effort(request, job)
    return MediaUploadResponse(media_id=media_id, probe_job_id=job.id)


@router.get("/media/{media_id}")
async def get_media(media_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> MediaOut:
    media = await _get_media(session, media_id)
    artifacts = (
        await session.scalars(
            select(Artifact).where(Artifact.media_id == media_id).order_by(Artifact.created_at)
        )
    ).all()
    jobs = (await session.scalars(select(Job).where(Job.media_id == media_id).order_by(Job.created_at))).all()
    latest_per_type = {j.type: j for j in jobs}
    return MediaOut(
        id=media.id,
        original_filename=media.original_filename,
        size_bytes=media.size_bytes,
        probe_status=media.probe_status,
        created_at=media.created_at,
        artifacts=[ArtifactOut.model_validate(a) for a in artifacts],
        jobs=[JobSummary.model_validate(j) for j in latest_per_type.values()],
    )


@router.get("/media/{media_id}/metadata")
async def get_media_metadata(
    media_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> MediaMetadataOut:
    media = await _get_media(session, media_id)
    if media.probe_status != "done":
        raise HTTPException(409, "probe not complete")
    return MediaMetadataOut.model_validate(media)


async def _probed_media(session: AsyncSession, media_id: uuid.UUID) -> Media:
    media = await _get_media(session, media_id)
    if media.probe_status != "done":
        raise HTTPException(409, "media has not been probed yet")
    return media


async def _submit(
    request: Request, session: AsyncSession, media_id: uuid.UUID, job_type: str, config: dict[str, Any]
) -> JobAccepted:
    job, created = await service.create_or_get(session, media_id, job_type, config)
    if created:
        await _enqueue_best_effort(request, job)
    return JobAccepted(job_id=job.id, status=job.status)


@router.post("/media/{media_id}/proxy", status_code=202)
async def request_proxy(
    media_id: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> JobAccepted:
    await _probed_media(session, media_id)
    return await _submit(request, session, media_id, "proxy", {"profile": "PROXY_STANDARD"})


@router.post("/media/{media_id}/analyze", status_code=202)
async def request_analysis(
    media_id: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> JobAccepted:
    await _probed_media(session, media_id)
    return await _submit(request, session, media_id, "analyze", {"version": 1})


@router.post("/media/{media_id}/edit", status_code=202)
async def request_edit(
    media_id: uuid.UUID, body: EditRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> JobAccepted:
    await _get_media(session, media_id)
    analyzed = await session.scalar(
        select(Artifact.id).where(Artifact.media_id == media_id, Artifact.type == "ANALYSIS")
    )
    if analyzed is None:
        raise HTTPException(409, "media has not been analyzed yet")
    return await _submit(request, session, media_id, "edit", {"request": body.normalized()})
