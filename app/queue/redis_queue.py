"""Redis carries only job ids. Losing a message is harmless: the sweeper re-enqueues from Postgres,
and duplicate deliveries are no-ops because claiming a job is an atomic Postgres UPDATE."""

import json
import uuid
from collections.abc import Awaitable
from typing import Any, cast

from redis.asyncio import Redis

QUEUES: dict[str, str] = {
    "probe": "media.probe",
    "proxy": "media.proxy",
    "analyze": "media.analysis",
    "edit": "media.edit",
    "export": "media.export",
}
_JOB_TYPE_BY_QUEUE = {queue: job_type for job_type, queue in QUEUES.items()}


def progress_channel(job_id: uuid.UUID) -> str:
    return f"jobs:{job_id}"


async def enqueue(redis: Redis, job_type: str, job_id: uuid.UUID) -> None:
    # redis-py types commands as sync|async unions; the asyncio client always returns awaitables.
    await cast(Awaitable[int], redis.lpush(QUEUES[job_type], str(job_id)))


async def dequeue(redis: Redis, timeout_s: float = 5) -> tuple[str, str] | None:
    """Returns (job_type, raw_job_id); the id is left unparsed so the caller can log a malformed one."""
    item = await cast(Awaitable[list[bytes] | None], redis.brpop(list(QUEUES.values()), timeout=timeout_s))
    if item is None:
        return None
    queue, raw = item
    return _JOB_TYPE_BY_QUEUE[queue.decode()], raw.decode()


async def publish_progress(redis: Redis, job_id: uuid.UUID, payload: dict[str, Any]) -> None:
    await redis.publish(progress_channel(job_id), json.dumps(payload, default=str))
