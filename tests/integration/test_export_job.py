import uuid

import pytest
from sqlalchemy import select

from app.jobs import service
from app.jobs.errors import ErrorCode, JobError
from app.jobs.states import JobStatus
from app.media.ffprobe import probe
from app.models import Artifact, EditPlan, Export, Job, JobAttempt, Media
from app.queue import redis_queue
from app.schemas.edl import EDL, TrimOp
from app.workers import steps
from app.workers.edit import edl_key
from tests.fixtures.make_videos import make_scenes_video, make_video


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    d = tmp_path_factory.mktemp("export_videos")
    return {
        "scenes": make_scenes_video(d / "scenes.mp4"),
        "noaudio": make_video(d / "noaudio.mp4", seconds=6, audio=False),
    }


@pytest.fixture
def export_factory(session_factory, storage, redis):
    async def make(src, ops) -> tuple[Export, Job]:
        mid = uuid.uuid4()
        key = f"media/{mid}/source.mp4"
        p = storage.path(key)
        p.parent.mkdir(parents=True)
        p.write_bytes(src.read_bytes())
        meta = await probe(p)
        edl = EDL(source_media_id=mid, operations=[TrimOp(start=s, end=e) for s, e in ops])
        async with session_factory() as s:
            s.add(Media(id=mid, original_filename="x.mp4", storage_key=key, size_bytes=p.stat().st_size,
                        duration_seconds=meta.duration_seconds, width=meta.width, height=meta.height,
                        video_codec=meta.video_codec, audio_codec=meta.audio_codec,
                        probe_status="done"))  # fmt: skip
            await s.flush()
            plan = EditPlan(media_id=mid, request="r", candidate_data={}, decisions=[],
                            accepted_operations=[o.model_dump() for o in edl.operations])  # fmt: skip
            s.add(plan)
            await s.commit()
        ek = edl_key(mid, plan.id)
        storage.path(ek).parent.mkdir(parents=True)
        storage.path(ek).write_text(edl.model_dump_json())
        async with session_factory() as s:
            job, _ = await service.create_or_get(s, mid, "export", {"edit_plan_id": str(plan.id)})
            export = Export(edit_plan_id=plan.id, media_id=mid, job_id=job.id)
            s.add(export)
            await s.commit()
        await redis_queue.enqueue(redis, "export", job.id)
        return export, job

    return make


async def _reload(session_factory, export, job) -> tuple[Export, Job]:
    async with session_factory() as s:
        return await s.get_one(Export, export.id), await s.get_one(Job, job.id)


async def _retry_now(session_factory, redis, worker, job):
    async with session_factory() as s:
        j = await s.get_one(Job, job.id)
        j.next_attempt_at = None
        await s.commit()
    await redis_queue.enqueue(redis, "export", job.id)
    assert await worker.run_once()


async def test_export_renders_edl(session_factory, redis, storage, worker, export_factory, videos):
    export, job = await export_factory(videos["scenes"], [(1.0, 3.0), (9.0, 11.5)])

    assert await worker.run_once()

    export, job = await _reload(session_factory, export, job)
    assert job.status == JobStatus.SUCCEEDED, job.error_message
    async with session_factory() as s:
        artifact = await s.get_one(Artifact, export.artifact_id)
    meta = await probe(storage.path(artifact.storage_key))
    assert meta.duration_seconds == pytest.approx(4.5, abs=0.25)
    assert meta.video_codec == "h264"
    assert (export.width, export.height, export.video_codec) == (640, 360, "h264")
    assert export.duration_seconds == pytest.approx(4.5, abs=0.25)
    assert export.status == "READY"
    assert artifact.type == "EXPORT"


async def test_video_without_audio_exports(session_factory, redis, storage, worker, export_factory, videos):
    export, job = await export_factory(videos["noaudio"], [(0.5, 2.0), (3.0, 5.0)])
    assert await worker.run_once()
    export, job = await _reload(session_factory, export, job)
    assert job.status == JobStatus.SUCCEEDED, job.error_message
    async with session_factory() as s:
        artifact = await s.get_one(Artifact, export.artifact_id)
    assert (await probe(storage.path(artifact.storage_key))).audio_codec is None


async def test_encoder_failure_recovers_with_fallback_profile(
    session_factory, redis, storage, worker, export_factory, videos, monkeypatch
):
    """Spec failure test 7: the export fails once and succeeds on retry."""
    real = steps.run_ffmpeg
    calls = []

    async def flaky(args, **kw):
        calls.append(kw["log_path"].name)
        if len(calls) == 2:  # second segment of the first attempt; real ffmpeg would have logged first
            kw["log_path"].write_text("Error while opening encoder for output stream #0:0")
            raise JobError(ErrorCode.ENCODER_FAILURE, "Error while opening encoder")
        return await real(args, **kw)

    monkeypatch.setattr(steps, "run_ffmpeg", flaky)
    export, job = await export_factory(videos["scenes"], [(1.0, 3.0), (5.0, 7.0)])

    assert await worker.run_once()
    _, job = await _reload(session_factory, export, job)
    assert (job.status, job.error_code) == (JobStatus.RETRYING, "ENCODER_FAILURE")
    # No Jev in this worker: the recovery fallback ladder picks the fallback codec on attempt 1.
    assert job.config["profile_override"] == "EXPORT_FALLBACK_CODEC"

    await _retry_now(session_factory, redis, worker, job)

    export, job = await _reload(session_factory, export, job)
    assert (job.status, job.attempt) == (JobStatus.SUCCEEDED, 2)
    async with session_factory() as s:
        attempts = (await s.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id))).all()
        logs = (await s.scalars(select(Artifact).where(Artifact.type == "LOG"))).all()
    assert sorted(a.outcome for a in attempts) == ["RETRYING:RETRY_FALLBACK_CODEC", "SUCCEEDED"]
    assert len(logs) == 1  # the failed attempt's ffmpeg log is kept


async def test_retry_resumes_from_first_missing_segment(
    session_factory, redis, storage, worker, export_factory, videos, monkeypatch
):
    real = steps.run_ffmpeg
    steps_run: list[str] = []

    async def crash_on_third(args, **kw):
        steps_run.append(kw["log_path"].name.split("-a")[-1])
        if len(steps_run) == 3:
            raise JobError(ErrorCode.TIMEOUT, "killed")  # plain retry, same profile
        return await real(args, **kw)

    monkeypatch.setattr(steps, "run_ffmpeg", crash_on_third)
    export, job = await export_factory(videos["scenes"], [(0.5, 1.5), (4.5, 5.5), (8.5, 9.5)])

    assert await worker.run_once()
    await _retry_now(session_factory, redis, worker, job)

    _, job = await _reload(session_factory, export, job)
    assert job.status == JobStatus.SUCCEEDED
    # Attempt 1 encoded segments 0 and 1, then died on 2; attempt 2 only encodes 2 and concatenates.
    assert steps_run == ["1-segment-0.log", "1-segment-1.log", "1-segment-2.log",
                         "2-segment-2.log", "2-concat.log"]  # fmt: skip


async def test_invalid_edl_fails_without_retry(
    session_factory, redis, storage, worker, export_factory, videos
):
    export, job = await export_factory(videos["noaudio"], [(0.5, 2.0)])
    async with session_factory() as s:
        plan_id = (await s.get_one(Export, export.id)).edit_plan_id
    storage.path(edl_key(export.media_id, plan_id)).write_text('{"version": 1, "operations": []}')

    assert await worker.run_once()

    _, job = await _reload(session_factory, export, job)
    assert (job.status, job.error_code, job.attempt) == (JobStatus.FAILED, "INVALID_EDL", 1)


async def test_export_endpoints_and_range_playback(
    settings, client_factory, session_factory, storage, worker, export_factory, videos
):
    export, job = await export_factory(videos["noaudio"], [(0.5, 2.0)])
    async with client_factory(settings) as client:
        pending = (await client.get(f"/exports/{export.id}")).json()
        assert await worker.run_once()
        ready = (await client.get(f"/exports/{export.id}")).json()
        full = await client.get(ready["download_url"])
        part = await client.get(ready["download_url"], headers={"Range": "bytes=0-99"})
        missing = await client.get(f"/exports/{uuid.uuid4()}")

    assert pending["status"] == "QUEUED"
    assert pending["download_url"] is None
    assert ready["status"] == "READY"
    assert ready["duration_seconds"] == pytest.approx(1.5, abs=0.25)
    assert full.status_code == 200
    assert full.headers["content-type"] == "video/mp4"
    assert part.status_code == 206
    assert len(part.content) == 100
    assert missing.status_code == 404
