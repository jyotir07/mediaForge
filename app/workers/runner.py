import asyncio
import logging
import os
import signal
import socket
import time
import uuid
from contextlib import suppress
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.llm import LLMClient, create_llm
from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.decision import DecisionLayer, create_decision_layer
from app.jobs import service
from app.jobs.errors import ErrorCode, JobError
from app.jobs.states import JobStatus, Stage
from app.logging import configure_logging, log_event
from app.models import Job, Media
from app.queue import redis_queue
from app.storage.local import Storage
from app.workers import analysis, edit, export, probe, proxy  # noqa: F401 - registers handlers
from app.workers.context import HANDLERS, JobContext, LeaseLost
from app.workers.recovery import decide_retry


class Worker:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        storage: Storage,
        worker_id: str | None = None,
        lease_s: int = service.DEFAULT_LEASE_S,
        heartbeat_s: float = 10,
        sweep_s: float = 15,
        dequeue_timeout_s: float = 5,
        llm: LLMClient | None = None,
        decisions: DecisionLayer | None = None,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.redis = redis
        self.storage = storage
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.lease_s = lease_s
        self.heartbeat_s = heartbeat_s
        self.sweep_s = sweep_s
        self.dequeue_timeout_s = dequeue_timeout_s
        self.llm = llm
        self.decisions = decisions or DecisionLayer.with_jev(None)

    async def run(self, stop: asyncio.Event) -> None:
        log_event("worker.started", worker_id=self.worker_id)
        sweeper = asyncio.create_task(self._sweep_forever(stop))
        try:
            while not stop.is_set():
                try:
                    await self.run_once()
                except Exception:
                    # Infra hiccup (Redis/Postgres blip): log, back off briefly, keep consuming.
                    log_event(
                        "worker.loop_error", level=logging.ERROR, exc_info=True, worker_id=self.worker_id
                    )
                    await asyncio.sleep(1)
        finally:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper
            log_event("worker.stopped", worker_id=self.worker_id)

    async def run_once(self) -> bool:
        """Process at most one queued message. Returns False if the queue was empty."""
        item = await redis_queue.dequeue(self.redis, self.dequeue_timeout_s)
        if item is None:
            return False
        job_type, raw_id = item
        try:
            job_id = uuid.UUID(raw_id)
        except ValueError:
            log_event("worker.malformed_message", level=logging.WARNING, job_type=job_type, raw=raw_id)
            return True
        await self.process(job_id)
        return True

    async def sweep_once(self) -> None:
        async with self.session_factory() as s:
            recovered = await service.recover_expired_leases(s)
        for job_id in recovered:
            log_event("job.lease_expired", level=logging.WARNING, job_id=job_id)
        async with self.session_factory() as s:
            due = await service.due_jobs(s)
        for job_id, job_type in due:
            await redis_queue.enqueue(self.redis, job_type, job_id)

    async def _sweep_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.sweep_once()
            except Exception:
                log_event("worker.sweep_error", level=logging.ERROR, exc_info=True, worker_id=self.worker_id)
            await asyncio.sleep(self.sweep_s)

    async def process(self, job_id: uuid.UUID) -> None:
        async with self.session_factory() as s:
            job = await service.claim(s, job_id, self.worker_id, self.lease_s)
            if job is None:
                log_event("job.claim_skipped", job_id=job_id, worker_id=self.worker_id)
                return
            media = await s.get_one(Media, job.media_id)

        started = time.monotonic()
        log_event("job.started", job_id=job.id, type=job.type, attempt=job.attempt, worker_id=self.worker_id)
        await self._publish(job, JobStatus.RUNNING)
        ctx = JobContext(
            job=job,
            media=media,
            settings=self.settings,
            storage=self.storage,
            session_factory=self.session_factory,
            redis=self.redis,
            worker_id=self.worker_id,
            llm=self.llm,
            decisions=self.decisions,
        )
        work = asyncio.create_task(HANDLERS[job.type](ctx))
        lease_lost = asyncio.Event()
        beat = asyncio.create_task(self._heartbeat(job.id, work, lease_lost))
        try:
            result = await work
        except asyncio.CancelledError:
            if not lease_lost.is_set():
                raise  # this worker is shutting down; lease expiry hands the job to another worker
            log_event("job.lease_lost", level=logging.WARNING, job_id=job.id, worker_id=self.worker_id)
            return
        except LeaseLost:
            log_event("job.lease_lost", level=logging.WARNING, job_id=job.id, worker_id=self.worker_id)
            return
        except JobError as err:
            await self._fail(job, media, err, started)
        except Exception as exc:
            log_event("job.crashed", level=logging.ERROR, exc_info=True, job_id=job.id, type=job.type)
            await self._fail(job, media, JobError(ErrorCode.PROCESS_INTERRUPTED, repr(exc)[:500]), started)
        else:
            async with self.session_factory() as s:
                ok = await service.succeed(s, job.id, self.worker_id, result)
            if ok:
                log_event(
                    "job.completed",
                    job_id=job.id,
                    type=job.type,
                    attempt=job.attempt,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                await self._publish(job, JobStatus.SUCCEEDED, stage=Stage.COMPLETED, progress=1.0)
        finally:
            beat.cancel()
            if not work.done():
                work.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await work
            with suppress(asyncio.CancelledError):
                await beat

    async def _fail(self, job: Job, media: Media, err: JobError, started: float) -> None:
        decision = await decide_retry(err, job, media, self.decisions.recovery)
        async with self.session_factory() as s:
            status = await service.fail(s, job.id, self.worker_id, err, decision)
        if status is None:
            return
        log_event(
            "job.failed_attempt",
            level=logging.WARNING,
            job_id=job.id,
            type=job.type,
            attempt=job.attempt,
            error_code=err.code,
            new_status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        await self._publish(job, status, error_code=err.code, error_message=err.message[:500])

    async def _heartbeat(self, job_id: uuid.UUID, work: asyncio.Task[Any], lease_lost: asyncio.Event) -> None:
        while not work.done():
            await asyncio.sleep(self.heartbeat_s)
            async with self.session_factory() as s:
                alive = await service.heartbeat(s, job_id, self.worker_id, self.lease_s)
            if not alive:
                lease_lost.set()
                work.cancel()
                return

    async def _publish(self, job: Job, status: JobStatus, **extra: Any) -> None:
        try:
            await redis_queue.publish_progress(
                self.redis, job.id, {"job_id": job.id, "status": status, **extra}
            )
        except Exception:
            # Progress fanout is best-effort; Postgres already holds the state.
            log_event("job.publish_failed", level=logging.WARNING, exc_info=True, job_id=job.id)


async def main() -> None:
    configure_logging()
    settings = get_settings()
    engine = create_engine(settings)
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2)
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    worker = Worker(
        settings,
        create_session_factory(engine),
        redis,
        Storage(settings.storage_root),
        llm=create_llm(settings),
        decisions=create_decision_layer(settings),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await worker.run(stop)
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
