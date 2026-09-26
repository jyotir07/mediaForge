import asyncio
import uuid

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text

from app.jobs import service
from app.jobs.states import JobStatus
from app.models import Job, JobAttempt, Media
from app.queue import redis_queue
from app.storage.local import Storage
from app.workers import context
from app.workers.runner import Worker
from tests.fixtures.make_videos import make_video


@pytest.fixture
async def redis(settings):
    r = Redis.from_url(settings.redis_url)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest.fixture
def storage(settings) -> Storage:
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    return Storage(settings.storage_root)


@pytest.fixture
def worker(settings, session_factory, redis, storage) -> Worker:
    return Worker(settings, session_factory, redis, storage, lease_s=2, heartbeat_s=0.2, dequeue_timeout_s=1)


@pytest.fixture
def sample_video(tmp_path):
    return make_video(tmp_path / "sample.mp4")


async def _media_with_source(session_factory, storage, src_bytes: bytes | None, ext=".mp4") -> Media:
    mid = uuid.uuid4()
    key = f"media/{mid}/source{ext}"
    if src_bytes is not None:
        p = storage.path(key)
        p.parent.mkdir(parents=True)
        p.write_bytes(src_bytes)
    async with session_factory() as s:
        m = Media(id=mid, original_filename=f"x{ext}", storage_key=key, size_bytes=len(src_bytes or b""))
        s.add(m)
        await s.commit()
    return m


async def _queued_probe(session_factory, redis, media_id) -> uuid.UUID:
    async with session_factory() as s:
        job, _ = await service.create_or_get(s, media_id, "probe", {})
    await redis_queue.enqueue(redis, "probe", job.id)
    return job.id


async def _job(session_factory, job_id) -> Job:
    async with session_factory() as s:
        return await s.get_one(Job, job_id)


async def _attempts(session_factory, job_id) -> list[JobAttempt]:
    async with session_factory() as s:
        q = select(JobAttempt).where(JobAttempt.job_id == job_id).order_by(JobAttempt.attempt)
        return list(await s.scalars(q))


async def _age_job(session_factory, job_id, seconds: int):
    async with session_factory() as s:
        await s.execute(
            text(
                "UPDATE jobs SET updated_at = updated_at - make_interval(secs => :s),"
                " next_attempt_at = next_attempt_at - make_interval(secs => :s),"
                " lease_expires_at = lease_expires_at - make_interval(secs => :s) WHERE id = :id"
            ),
            {"s": seconds, "id": job_id},
        )
        await s.commit()


async def test_upload_enqueues_probe_and_worker_fills_metadata(
    settings, client_factory, worker, sample_video
):
    async with client_factory(settings) as client:
        resp = await client.post("/media", params={"filename": "a.mp4"}, content=sample_video.read_bytes())
        body = resp.json()
        assert body["probe_job_id"] is not None

        assert await worker.run_once()

        job = (await client.get(f"/media/{body['media_id']}")).json()["jobs"][0]
        meta = await client.get(f"/media/{body['media_id']}/metadata")

    assert job["status"] == "SUCCEEDED"
    assert meta.status_code == 200
    assert (meta.json()["width"], meta.json()["height"]) == (320, 240)


async def test_duplicate_delivery_runs_job_once(session_factory, redis, storage, worker, sample_video):
    media = await _media_with_source(session_factory, storage, sample_video.read_bytes())
    job_id = await _queued_probe(session_factory, redis, media.id)
    await redis_queue.enqueue(redis, "probe", job_id)

    assert await worker.run_once()
    assert await worker.run_once()

    assert (await _job(session_factory, job_id)).status == JobStatus.SUCCEEDED
    assert len(await _attempts(session_factory, job_id)) == 1


async def test_worker_dying_mid_job_is_recovered_by_sweeper(
    session_factory, redis, storage, worker, sample_video, monkeypatch
):
    calls = 0
    started = asyncio.Event()
    real_probe = context.HANDLERS["probe"]

    async def flaky_probe(ctx):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.sleep(3600)
        return await real_probe(ctx)

    monkeypatch.setitem(context.HANDLERS, "probe", flaky_probe)
    media = await _media_with_source(session_factory, storage, sample_video.read_bytes())
    job_id = await _queued_probe(session_factory, redis, media.id)

    crashed = asyncio.create_task(worker.run_once())
    await started.wait()
    crashed.cancel()  # the worker process dies: no failure is ever written
    with pytest.raises(asyncio.CancelledError):
        await crashed
    assert (await _job(session_factory, job_id)).status == JobStatus.RUNNING

    await _age_job(session_factory, job_id, 3600)
    await worker.sweep_once()  # lease expired -> RETRYING
    await _age_job(session_factory, job_id, 3600)
    await worker.sweep_once()  # backoff elapsed -> re-enqueued
    assert await worker.run_once()

    assert (await _job(session_factory, job_id)).status == JobStatus.SUCCEEDED
    attempts = await _attempts(session_factory, job_id)
    assert [a.error_code for a in attempts] == ["WORKER_LOST", None]


async def test_missing_source_fails_without_retry(session_factory, redis, storage, worker):
    media = await _media_with_source(session_factory, storage, None)
    job_id = await _queued_probe(session_factory, redis, media.id)

    assert await worker.run_once()

    job = await _job(session_factory, job_id)
    assert (job.status, job.error_code, job.attempt) == (JobStatus.FAILED, "MISSING_SOURCE", 1)


async def test_corrupt_source_fails_without_retry(session_factory, redis, storage, worker):
    media = await _media_with_source(session_factory, storage, b"\x00\x00\x00\x20ftypisom" + b"\x00" * 500)
    job_id = await _queued_probe(session_factory, redis, media.id)

    assert await worker.run_once()

    job = await _job(session_factory, job_id)
    assert (job.status, job.error_code, job.attempt) == (JobStatus.FAILED, "CORRUPT_SOURCE", 1)


async def test_unexpected_handler_exception_is_retried(
    session_factory, redis, storage, worker, sample_video, monkeypatch
):
    async def broken(ctx):
        raise RuntimeError("bug")

    monkeypatch.setitem(context.HANDLERS, "probe", broken)
    media = await _media_with_source(session_factory, storage, sample_video.read_bytes())
    job_id = await _queued_probe(session_factory, redis, media.id)

    assert await worker.run_once()

    job = await _job(session_factory, job_id)
    assert (job.status, job.error_code) == (JobStatus.RETRYING, "PROCESS_INTERRUPTED")


async def test_redis_data_loss_is_recovered_from_postgres(
    session_factory, redis, storage, worker, sample_video
):
    media = await _media_with_source(session_factory, storage, sample_video.read_bytes())
    job_id = await _queued_probe(session_factory, redis, media.id)
    await redis.flushdb()

    assert not await worker.run_once()  # nothing in the queue any more
    await _age_job(session_factory, job_id, 60)
    await worker.sweep_once()
    assert await worker.run_once()

    assert (await _job(session_factory, job_id)).status == JobStatus.SUCCEEDED


async def test_lost_lease_cancels_handler_and_writes_nothing(
    session_factory, redis, storage, worker, sample_video, monkeypatch
):
    async def slow(ctx):
        await asyncio.sleep(3600)
        return {}

    monkeypatch.setitem(context.HANDLERS, "probe", slow)
    media = await _media_with_source(session_factory, storage, sample_video.read_bytes())
    job_id = await _queued_probe(session_factory, redis, media.id)

    run = asyncio.create_task(worker.run_once())
    await asyncio.sleep(0.3)
    async with session_factory() as s:  # another worker took the job over
        await s.execute(text("UPDATE jobs SET worker_id = 'someone-else' WHERE id = :id"), {"id": job_id})
        await s.commit()

    assert await asyncio.wait_for(run, timeout=5)
    job = await _job(session_factory, job_id)
    assert (job.status, job.worker_id) == (JobStatus.RUNNING, "someone-else")
