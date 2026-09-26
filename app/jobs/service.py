"""Durable job state. Postgres is the source of truth; every transition is a guarded UPDATE.

Timestamps come from the database clock (now()) so workers with skewed clocks agree on leases.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.errors import ErrorCode, JobError
from app.jobs.states import JobStatus, Stage, backoff_seconds
from app.models import Job, JobAttempt

ERROR_MESSAGE_MAX = 2000
DEFAULT_LEASE_S = 60


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    profile_override: str | None = None
    strategy: str | None = None


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def idempotency_key(media_id: uuid.UUID, job_type: str, config: dict[str, Any]) -> str:
    return hashlib.sha256(f"{media_id}:{job_type}:{canonical_json(config)}".encode()).hexdigest()


def _owned(job_id: uuid.UUID, worker_id: str) -> Any:
    return and_(Job.id == job_id, Job.status == JobStatus.RUNNING, Job.worker_id == worker_id)


async def create_or_get(
    s: AsyncSession,
    media_id: uuid.UUID,
    job_type: str,
    config: dict[str, Any],
    max_attempts: int = 3,
) -> tuple[Job, bool]:
    key = idempotency_key(media_id, job_type, config)
    # A conflicting live job can turn FAILED between our INSERT and SELECT; a couple of rounds settles it.
    for _ in range(3):
        insert = (
            pg_insert(Job)
            .values(
                id=uuid.uuid4(),
                media_id=media_id,
                type=job_type,
                status=JobStatus.QUEUED,
                idempotency_key=key,
                config=config,
                max_attempts=max_attempts,
            )
            # Literal predicate: Postgres can only infer a partial index from a matching constant WHERE.
            .on_conflict_do_nothing(
                index_elements=[Job.idempotency_key], index_where=text("status <> 'FAILED'")
            )
            .returning(Job)
        )
        created = (await s.scalars(insert)).one_or_none()
        if created is not None:
            await s.commit()
            return created, True
        existing = await s.scalar(
            select(Job).where(Job.idempotency_key == key, Job.status != JobStatus.FAILED)
        )
        await s.commit()
        if existing is not None:
            return existing, False
    raise RuntimeError(f"could not create or find job for key {key}")


async def claim(
    s: AsyncSession, job_id: uuid.UUID, worker_id: str, lease_s: int = DEFAULT_LEASE_S
) -> Job | None:
    stmt = (
        update(Job)
        .where(
            Job.id == job_id,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RETRYING]),
            or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= func.now()),
        )
        .values(
            status=JobStatus.RUNNING,
            attempt=Job.attempt + 1,
            lease_expires_at=func.now() + timedelta(seconds=lease_s),
            worker_id=worker_id,
            started_at=func.coalesce(Job.started_at, func.now()),
            next_attempt_at=None,
            error_code=None,
            error_message=None,
            updated_at=func.now(),
        )
        .returning(Job)
        .execution_options(populate_existing=True)
    )
    job = (await s.scalars(stmt)).one_or_none()
    if job is None:
        await s.commit()  # nothing changed; commit ends the txn without expiring caller objects
        return None
    s.add(JobAttempt(job_id=job.id, attempt=job.attempt, worker_id=worker_id))
    await s.commit()
    return job


async def heartbeat(
    s: AsyncSession, job_id: uuid.UUID, worker_id: str, lease_s: int = DEFAULT_LEASE_S
) -> bool:
    stmt = (
        update(Job)
        .where(_owned(job_id, worker_id))
        .values(lease_expires_at=func.now() + timedelta(seconds=lease_s), updated_at=func.now())
        .returning(Job.id)
    )
    alive = (await s.execute(stmt)).scalar_one_or_none() is not None
    await s.commit()
    return alive


async def set_progress(
    s: AsyncSession, job_id: uuid.UUID, worker_id: str, stage: Stage, progress: float, message: str
) -> bool:
    stmt = (
        update(Job)
        .where(_owned(job_id, worker_id))
        .values(
            stage=stage, progress=max(0.0, min(1.0, progress)), message=message[:500], updated_at=func.now()
        )
        .returning(Job.id)
    )
    alive = (await s.execute(stmt)).scalar_one_or_none() is not None
    await s.commit()
    return alive


async def _close_attempt(
    s: AsyncSession,
    job_id: uuid.UUID,
    attempt: int,
    outcome: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    await s.execute(
        update(JobAttempt)
        .where(JobAttempt.job_id == job_id, JobAttempt.attempt == attempt, JobAttempt.ended_at.is_(None))
        .values(ended_at=func.now(), outcome=outcome, error_code=error_code, error_message=error_message)
    )


async def succeed(s: AsyncSession, job_id: uuid.UUID, worker_id: str, result: dict[str, Any]) -> bool:
    stmt = (
        update(Job)
        .where(_owned(job_id, worker_id))
        .values(
            status=JobStatus.SUCCEEDED,
            stage=Stage.COMPLETED,
            progress=1.0,
            result=result,
            completed_at=func.now(),
            lease_expires_at=None,
            updated_at=func.now(),
        )
        .returning(Job.attempt)
    )
    attempt = (await s.execute(stmt)).scalar_one_or_none()
    if attempt is None:
        await s.commit()
        return False
    await _close_attempt(s, job_id, attempt, JobStatus.SUCCEEDED)
    await s.commit()
    return True


async def _apply_failure(s: AsyncSession, job: Job, err: JobError, decision: RetryDecision) -> JobStatus:
    message = err.message[:ERROR_MESSAGE_MAX]
    retry = decision.retry and job.attempt < job.max_attempts
    values: dict[str, Any] = {
        "error_code": err.code,
        "error_message": message,
        "lease_expires_at": None,
        "updated_at": func.now(),
    }
    if retry:
        status = JobStatus.RETRYING
        values["next_attempt_at"] = func.now() + timedelta(seconds=backoff_seconds(job.attempt))
        if decision.profile_override:
            values["config"] = {**job.config, "profile_override": decision.profile_override}
    else:
        status = JobStatus.FAILED
        values["completed_at"] = func.now()
    values["status"] = status

    await s.execute(update(Job).where(Job.id == job.id).values(**values))
    outcome = f"{status}:{decision.strategy}" if decision.strategy else str(status)
    await _close_attempt(s, job.id, job.attempt, outcome, err.code, message)
    return status


async def fail(
    s: AsyncSession, job_id: uuid.UUID, worker_id: str, err: JobError, decision: RetryDecision
) -> JobStatus | None:
    """Record a failed attempt. Returns the new status, or None if this worker no longer owns the job."""
    job = await s.scalar(
        select(Job)
        .where(_owned(job_id, worker_id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None:
        await s.commit()
        return None
    status = await _apply_failure(s, job, err, decision)
    await s.commit()
    return status


async def recover_expired_leases(s: AsyncSession) -> list[uuid.UUID]:
    """Jobs whose worker stopped heartbeating are treated as a crashed attempt (WORKER_LOST)."""
    jobs = (
        await s.scalars(
            select(Job)
            .where(Job.status == JobStatus.RUNNING, Job.lease_expires_at < func.now())
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).all()
    err = JobError(ErrorCode.WORKER_LOST, "worker lease expired")
    for job in jobs:
        await _apply_failure(s, job, err, RetryDecision(retry=True))
    await s.commit()
    return [j.id for j in jobs]


async def due_jobs(s: AsyncSession, stale_queued_after_s: int = 30) -> list[tuple[uuid.UUID, str]]:
    """Jobs that should be (re)enqueued: retries whose backoff elapsed, and QUEUED jobs that sat unclaimed
    long enough that their Redis message may be lost. Touching updated_at rate-limits re-enqueueing."""
    stale = func.now() - timedelta(seconds=stale_queued_after_s)
    stmt = (
        update(Job)
        .where(
            or_(
                and_(Job.status == JobStatus.QUEUED, Job.updated_at < stale),
                and_(
                    Job.status == JobStatus.RETRYING,
                    Job.next_attempt_at <= func.now(),
                    or_(Job.updated_at < Job.next_attempt_at, Job.updated_at < stale),
                ),
            )
        )
        .values(updated_at=func.now())
        .returning(Job.id, Job.type)
        .execution_options(synchronize_session=False)
    )
    rows = (await s.execute(stmt)).all()
    await s.commit()
    return [(row.id, row.type) for row in rows]
