import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import get_session
from app.jobs.states import TERMINAL, JobStatus
from app.models import Job
from app.queue.redis_queue import progress_channel
from app.schemas.job import JobOut

router = APIRouter()

KEEPALIVE_S = 15.0
# Pub/sub is fire-and-forget; re-reading Postgres bounds how long a missed terminal event can stall a client.
DB_RECHECK_S = 3.0


def _sse(payload: dict[str, Any]) -> str:
    return f"event: progress\ndata: {json.dumps(payload, default=str)}\n\n"


async def _load(session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> Job | None:
    async with session_factory() as s:
        return await s.get(Job, job_id)


@router.get("/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> JobOut:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return JobOut.from_job(job)


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: uuid.UUID, request: Request) -> StreamingResponse:
    session_factory = request.app.state.session_factory
    if await _load(session_factory, job_id) is None:
        raise HTTPException(404, "job not found")

    # Subscribe before reading the snapshot so no update can fall between the two.
    pubsub = request.app.state.redis.pubsub()
    await pubsub.subscribe(progress_channel(job_id))

    async def stream() -> AsyncIterator[str]:
        try:
            job = await _load(session_factory, job_id)
            if job is None:
                return
            yield _sse(JobOut.from_job(job).model_dump(mode="json"))
            if JobStatus(job.status) in TERMINAL:
                return
            last_sent = last_check = time.monotonic()
            while not await request.is_disconnected():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                now = time.monotonic()
                if msg is not None:
                    payload = json.loads(msg["data"])
                    yield _sse(payload)
                    last_sent = now
                    if payload.get("status") in TERMINAL:
                        return
                if now - last_check >= DB_RECHECK_S:
                    last_check = now
                    job = await _load(session_factory, job_id)
                    if job is not None and JobStatus(job.status) in TERMINAL:
                        yield _sse(JobOut.from_job(job).model_dump(mode="json"))
                        return
                if now - last_sent >= KEEPALIVE_S:
                    last_sent = now
                    yield ": keepalive\n\n"
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
