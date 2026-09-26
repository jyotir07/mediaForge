import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.llm import LLMClient
from app.config import Settings
from app.decision import DecisionLayer
from app.jobs import service
from app.jobs.states import JobStatus, Stage
from app.models import Job, Media
from app.queue.redis_queue import publish_progress
from app.storage.local import Storage

PROGRESS_MIN_INTERVAL_S = 0.25


class LeaseLost(Exception):
    """This worker no longer owns the job; stop without writing anything."""


@dataclass
class JobContext:
    job: Job
    media: Media
    settings: Settings
    storage: Storage
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    worker_id: str
    llm: LLMClient | None = None
    decisions: DecisionLayer | None = None
    _last_progress_at: float = field(default=0.0, init=False)

    async def progress(self, stage: Stage, fraction: float, message: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and fraction < 1 and now - self._last_progress_at < PROGRESS_MIN_INTERVAL_S:
            return
        self._last_progress_at = now
        async with self.session_factory() as s:
            alive = await service.set_progress(s, self.job.id, self.worker_id, stage, fraction, message)
        if not alive:
            raise LeaseLost(str(self.job.id))
        await publish_progress(
            self.redis,
            self.job.id,
            {
                "job_id": self.job.id,
                "status": JobStatus.RUNNING,
                "stage": stage,
                "progress": fraction,
                "message": message,
            },
        )


Handler = Callable[[JobContext], Coroutine[Any, Any, dict[str, Any]]]
HANDLERS: dict[str, Handler] = {}


def handler(job_type: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[job_type] = fn
        return fn

    return register
