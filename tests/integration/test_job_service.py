import asyncio
import uuid

import pytest
from sqlalchemy import select, text

from app.jobs import service
from app.jobs.errors import ErrorCode, JobError
from app.jobs.service import RetryDecision
from app.jobs.states import JobStatus
from app.models import Job, JobAttempt, Media


@pytest.fixture
async def media_id(session_factory) -> uuid.UUID:
    async with session_factory() as s:
        m = Media(original_filename="a.mp4", storage_key="media/a/source.mp4", size_bytes=1)
        s.add(m)
        await s.commit()
        return m.id


async def _job(session_factory, job_id) -> Job:
    async with session_factory() as s:
        return await s.get_one(Job, job_id)


async def _attempts(session_factory, job_id) -> list[JobAttempt]:
    async with session_factory() as s:
        return list(
            await s.scalars(
                select(JobAttempt).where(JobAttempt.job_id == job_id).order_by(JobAttempt.attempt)
            )
        )


async def _expire_lease(session_factory, job_id):
    async with session_factory() as s:
        await s.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )
        await s.commit()


async def _make_due(session_factory, job_id):
    """Simulate the backoff elapsing: shift the job's timestamps into the past, preserving their order."""
    async with session_factory() as s:
        await s.execute(
            text(
                "UPDATE jobs SET next_attempt_at = now() - interval '1 second',"
                " updated_at = now() - interval '2 seconds' WHERE id = :id"
            ),
            {"id": job_id},
        )
        await s.commit()


def test_idempotency_key_ignores_config_key_order():
    mid = uuid.uuid4()
    a = service.idempotency_key(mid, "proxy", {"profile": "X", "h": 1080})
    b = service.idempotency_key(mid, "proxy", {"h": 1080, "profile": "X"})
    assert a == b
    assert a != service.idempotency_key(mid, "proxy", {"profile": "Y", "h": 1080})


async def test_create_or_get_returns_existing_job_for_same_request(session_factory, media_id):
    async with session_factory() as s:
        first, created1 = await service.create_or_get(s, media_id, "probe", {})
    async with session_factory() as s:
        second, created2 = await service.create_or_get(s, media_id, "probe", {})
    assert (created1, created2) == (True, False)
    assert first.id == second.id
    assert first.status == JobStatus.QUEUED


async def test_concurrent_create_or_get_creates_exactly_one_job(session_factory, media_id):
    async def create():
        async with session_factory() as s:
            job, _ = await service.create_or_get(s, media_id, "proxy", {"profile": "PROXY_STANDARD"})
            return job.id

    ids = await asyncio.gather(*[create() for _ in range(20)])
    assert len(set(ids)) == 1
    async with session_factory() as s:
        count = await s.scalar(text("SELECT count(*) FROM jobs"))
    assert count == 1


async def test_concurrent_claims_exactly_one_wins(session_factory, media_id):
    async with session_factory() as s:
        job, _ = await service.create_or_get(s, media_id, "probe", {})

    async def claim(worker):
        async with session_factory() as s:
            return await service.claim(s, job.id, worker)

    results = await asyncio.gather(*[claim(f"w{i}") for i in range(10)])
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].status == JobStatus.RUNNING
    assert winners[0].attempt == 1
    assert len(await _attempts(session_factory, job.id)) == 1


async def test_succeed_marks_job_and_closes_attempt(session_factory, media_id):
    async with session_factory() as s:
        job, _ = await service.create_or_get(s, media_id, "probe", {})
        await service.claim(s, job.id, "w1")
        assert await service.succeed(s, job.id, "w1", {"ok": True})

    done = await _job(session_factory, job.id)
    assert done.status == JobStatus.SUCCEEDED
    assert done.progress == 1.0
    assert done.result == {"ok": True}
    assert done.completed_at is not None
    [attempt] = await _attempts(session_factory, job.id)
    assert attempt.outcome == "SUCCEEDED"
    assert attempt.ended_at is not None


async def test_retryable_failure_schedules_retry_then_blocks_early_claim(session_factory, media_id):
    async with session_factory() as s:
        job, _ = await service.create_or_get(s, media_id, "probe", {})
        await service.claim(s, job.id, "w1")
        status = await service.fail(
            s, job.id, "w1", JobError(ErrorCode.TIMEOUT, "slow"), RetryDecision(retry=True)
        )
        assert status == JobStatus.RETRYING
        assert await service.claim(s, job.id, "w2") is None  # backoff not elapsed

    retrying = await _job(session_factory, job.id)
    assert retrying.error_code == "TIMEOUT"
    assert retrying.next_attempt_at is not None
    [attempt] = await _attempts(session_factory, job.id)
    assert attempt.error_code == "TIMEOUT"
    assert attempt.outcome == "RETRYING"

    await _make_due(session_factory, job.id)
    async with session_factory() as s:
        again = await service.claim(s, job.id, "w2")
    assert again is not None and again.attempt == 2


async def test_retry_decision_profile_override_is_stored_in_config(session_factory, media_id):
    async with session_factory() as s:
        job, _ = await service.create_or_get(s, media_id, "export", {"edit_plan_id": "p1"})
        await service.claim(s, job.id, "w1")
        await service.fail(
            s,
            job.id,
            "w1",
            JobError(ErrorCode.ENCODER_FAILURE, "enc"),
            RetryDecision(
                retry=True, profile_override="EXPORT_FALLBACK_CODEC", strategy="RETRY_FALLBACK_CODEC"
            ),
        )
    job = await _job(session_factory, job.id)
    assert job.config == {"edit_plan_id": "p1", "profile_override": "EXPORT_FALLBACK_CODEC"}
    [attempt] = await _attempts(session_factory, job.id)
    assert attempt.outcome == "RETRYING:RETRY_FALLBACK_CODEC"


async def test_fatal_failure_fails_immediately(session_factory, media_id):
    async with session_factory() as s:
        job, _ = await service.create_or_get(s, media_id, "probe", {})
        await service.claim(s, job.id, "w1")
        status = await service.fail(
            s, job.id, "w1", JobError(ErrorCode.MISSING_SOURCE, "x" * 5000), RetryDecision(retry=False)
        )
    assert status == JobStatus.FAILED
    failed = await _job(session_factory, job.id)
    assert failed.error_code == "MISSING_SOURCE"
    assert len(failed.error_message) == 2000


async def test_retry_on_last_attempt_fails_instead(session_factory, media_id):
    async with session_factory() as s:
        job, _ = await service.create_or_get(s, media_id, "probe", {}, max_attempts=1)
        await service.claim(s, job.id, "w1")
        status = await service.fail(
            s, job.id, "w1", JobError(ErrorCode.TIMEOUT, "t"), RetryDecision(retry=True)
        )
    assert status == JobStatus.FAILED


async def test_expired_lease_is_recovered_as_worker_lost(session_factory, media_id):
    async with session_factory() as s:
        job, _ = await service.create_or_get(s, media_id, "probe", {})
        await service.claim(s, job.id, "w1")
    await _expire_lease(session_factory, job.id)

    async with session_factory() as s:
        recovered = await service.recover_expired_leases(s)

    assert recovered == [job.id]
    j = await _job(session_factory, job.id)
    assert j.status == JobStatus.RETRYING
    [attempt] = await _attempts(session_factory, job.id)
    assert attempt.error_code == "WORKER_LOST"
    assert attempt.ended_at is not None


async def test_expired_lease_on_last_attempt_fails(session_factory, media_id):
    async with session_factory() as s:
        job, _ = await service.create_or_get(s, media_id, "probe", {}, max_attempts=1)
        await service.claim(s, job.id, "w1")
    await _expire_lease(session_factory, job.id)
    async with session_factory() as s:
        await service.recover_expired_leases(s)
    j = await _job(session_factory, job.id)
    assert j.status == JobStatus.FAILED
    assert j.error_code == "WORKER_LOST"


async def test_stale_worker_cannot_overwrite_after_takeover(session_factory, media_id):
    async with session_factory() as s:
        job, _ = await service.create_or_get(s, media_id, "probe", {})
        await service.claim(s, job.id, "w1")
    await _expire_lease(session_factory, job.id)
    async with session_factory() as s:
        await service.recover_expired_leases(s)
    await _make_due(session_factory, job.id)
    async with session_factory() as s:
        assert await service.claim(s, job.id, "w2") is not None
        assert await service.heartbeat(s, job.id, "w1") is False
        assert await service.succeed(s, job.id, "w1", {"stale": True}) is False
        assert await service.heartbeat(s, job.id, "w2") is True

    j = await _job(session_factory, job.id)
    assert j.status == JobStatus.RUNNING
    assert j.worker_id == "w2"


async def test_failed_job_can_be_recreated_with_same_key(session_factory, media_id):
    async with session_factory() as s:
        job, _ = await service.create_or_get(s, media_id, "probe", {}, max_attempts=1)
        await service.claim(s, job.id, "w1")
        await service.fail(
            s, job.id, "w1", JobError(ErrorCode.CORRUPT_SOURCE, "bad"), RetryDecision(retry=False)
        )
        again, created = await service.create_or_get(s, media_id, "probe", {})
    assert created
    assert again.id != job.id


async def test_due_jobs_returns_due_retries_once_and_stale_queued(session_factory, media_id):
    async with session_factory() as s:
        fresh, _ = await service.create_or_get(s, media_id, "probe", {"n": 1})
        stale, _ = await service.create_or_get(s, media_id, "probe", {"n": 2})
        retry, _ = await service.create_or_get(s, media_id, "proxy", {"n": 3})
        await service.claim(s, retry.id, "w1")
        await service.fail(s, retry.id, "w1", JobError(ErrorCode.TIMEOUT, "t"), RetryDecision(retry=True))
        await s.execute(
            text("UPDATE jobs SET updated_at = now() - interval '1 minute' WHERE id = :id"), {"id": stale.id}
        )
        await s.commit()
    await _make_due(session_factory, retry.id)

    async with session_factory() as s:
        due = await service.due_jobs(s, stale_queued_after_s=30)
    assert sorted(due) == sorted([(stale.id, "probe"), (retry.id, "proxy")])
    assert fresh.id not in {i for i, _ in due}

    async with session_factory() as s:
        assert await service.due_jobs(s, stale_queued_after_s=30) == []
