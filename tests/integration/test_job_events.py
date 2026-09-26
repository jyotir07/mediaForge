import asyncio
import json
import uuid

import pytest

from app.jobs import service
from app.jobs.states import Stage
from app.models import Media
from app.queue.redis_queue import publish_progress


def parse_sse(body: str) -> list[dict]:
    events = []
    for block in body.split("\n\n"):
        data = [line[len("data: ") :] for line in block.splitlines() if line.startswith("data: ")]
        if data:
            events.append(json.loads("".join(data)))
    return events


@pytest.fixture
async def job(session_factory):
    async with session_factory() as s:
        m = Media(original_filename="a.mp4", storage_key="media/a/source.mp4", size_bytes=1)
        s.add(m)
        await s.commit()
        job, _ = await service.create_or_get(s, m.id, "probe", {})
    return job


async def test_get_job_returns_state(settings, client_factory, job):
    async with client_factory(settings) as client:
        resp = await client.get(f"/jobs/{job.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == str(job.id)
    assert body["status"] == "QUEUED"
    assert body["progress"] == 0.0
    assert body["type"] == "probe"


async def test_unknown_job_is_404(settings, client_factory):
    async with client_factory(settings) as client:
        assert (await client.get(f"/jobs/{uuid.uuid4()}")).status_code == 404
        assert (await client.get(f"/jobs/{uuid.uuid4()}/events")).status_code == 404


async def test_events_for_finished_job_send_snapshot_and_close(
    settings, client_factory, session_factory, job
):
    async with session_factory() as s:
        await service.claim(s, job.id, "w1")
        await service.succeed(s, job.id, "w1", {})

    async with client_factory(settings) as client:
        resp = await asyncio.wait_for(client.get(f"/jobs/{job.id}/events"), timeout=5)

    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    assert len(events) == 1
    assert events[0]["status"] == "SUCCEEDED"


async def test_events_stream_progress_until_terminal(settings, client_factory, session_factory, job):
    from redis.asyncio import Redis

    async with session_factory() as s:
        await service.claim(s, job.id, "w1")

    async def run_job():
        redis = Redis.from_url(settings.redis_url)
        await asyncio.sleep(0.5)  # let the SSE handler subscribe
        for p in (0.25, 0.5, 1.0):
            async with session_factory() as s:
                await service.set_progress(s, job.id, "w1", Stage.PROBE, p, "probing")
            await publish_progress(redis, job.id, {"job_id": job.id, "status": "RUNNING", "progress": p})
        async with session_factory() as s:
            await service.succeed(s, job.id, "w1", {})
        await publish_progress(redis, job.id, {"job_id": job.id, "status": "SUCCEEDED", "progress": 1.0})
        await redis.aclose()

    async with client_factory(settings) as client:
        producer = asyncio.create_task(run_job())
        resp = await asyncio.wait_for(client.get(f"/jobs/{job.id}/events"), timeout=10)
        await producer

    events = parse_sse(resp.text)
    progress = [e["progress"] for e in events]
    assert progress == sorted(progress)
    assert events[0]["status"] == "RUNNING"
    assert events[-1]["status"] == "SUCCEEDED"
    assert len(events) >= 4


async def test_events_terminate_from_postgres_when_publish_is_missed(
    settings, client_factory, session_factory, job
):
    async with session_factory() as s:
        await service.claim(s, job.id, "w1")

    async def finish_silently():
        await asyncio.sleep(0.5)
        async with session_factory() as s:
            await service.succeed(s, job.id, "w1", {})  # no Redis publish at all

    async with client_factory(settings) as client:
        finisher = asyncio.create_task(finish_silently())
        resp = await asyncio.wait_for(client.get(f"/jobs/{job.id}/events"), timeout=15)
        await finisher

    assert parse_sse(resp.text)[-1]["status"] == "SUCCEEDED"
