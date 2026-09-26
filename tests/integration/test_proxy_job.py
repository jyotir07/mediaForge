import uuid

import pytest
from sqlalchemy import select

from app.jobs import service
from app.jobs.states import JobStatus
from app.media.ffprobe import probe
from app.models import Artifact, Job, Media
from app.queue import redis_queue
from app.workers import steps
from tests.fixtures.make_videos import make_corrupt_video, make_scenes_video, make_video


@pytest.fixture(scope="session")
def videos(tmp_path_factory):
    d = tmp_path_factory.mktemp("proxy_videos")
    scenes = make_scenes_video(d / "scenes.mp4")
    return {
        "scenes": scenes,
        "noaudio": make_video(d / "noaudio.mp4", seconds=3, audio=False),
        "portrait": make_video(d / "portrait.mp4", seconds=2, size="360x640"),
        "corrupt": make_corrupt_video(d / "corrupt.mp4", scenes),
    }


async def _probed_media(session_factory, storage, src) -> Media:
    """A media row as it looks after a successful probe job."""
    mid = uuid.uuid4()
    key = f"media/{mid}/source.mp4"
    p = storage.path(key)
    p.parent.mkdir(parents=True)
    p.write_bytes(src.read_bytes())
    try:
        meta = await probe(p)
        fields = dict(duration_seconds=meta.duration_seconds, width=meta.width, height=meta.height,
                      audio_codec=meta.audio_codec, probe_status="done")  # fmt: skip
    except Exception:
        fields = dict(duration_seconds=12.0, width=640, height=360, audio_codec="aac", probe_status="done")
    async with session_factory() as s:
        m = Media(id=mid, original_filename="x.mp4", storage_key=key, size_bytes=p.stat().st_size, **fields)
        s.add(m)
        await s.commit()
    return m


async def _run_proxy_job(session_factory, redis, worker, media_id, config=None) -> Job:
    async with session_factory() as s:
        job, _ = await service.create_or_get(s, media_id, "proxy", config or {"profile": "PROXY_STANDARD"})
    await redis_queue.enqueue(redis, "proxy", job.id)
    assert await worker.run_once()
    async with session_factory() as s:
        return await s.get_one(Job, job.id)


async def _artifacts(session_factory, media_id, type_=None) -> list[Artifact]:
    async with session_factory() as s:
        q = select(Artifact).where(Artifact.media_id == media_id)
        if type_:
            q = q.where(Artifact.type == type_)
        return list(await s.scalars(q))


async def test_proxy_and_contact_sheet_generated(session_factory, redis, storage, worker, videos):
    media = await _probed_media(session_factory, storage, videos["scenes"])
    job = await _run_proxy_job(session_factory, redis, worker, media.id)

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    [proxy] = await _artifacts(session_factory, media.id, "PROXY")
    [thumb] = await _artifacts(session_factory, media.id, "THUMBNAIL")
    meta = await probe(storage.path(proxy.storage_key))
    assert meta.video_codec == "h264"
    assert (meta.width, meta.height) == (640, 360)  # never upscaled
    assert proxy.size_bytes == storage.path(proxy.storage_key).stat().st_size
    assert storage.path(thumb.storage_key).stat().st_size > 0
    leftovers = [p for p in storage.root.rglob(".tmp-*")]
    assert leftovers == []


async def test_rerun_after_success_reuses_outputs(
    session_factory, redis, storage, worker, videos, monkeypatch
):
    media = await _probed_media(session_factory, storage, videos["scenes"])
    await _run_proxy_job(session_factory, redis, worker, media.id)

    calls = 0
    real = steps.run_ffmpeg

    async def counting(*a, **kw):
        nonlocal calls
        calls += 1
        return await real(*a, **kw)

    monkeypatch.setattr(steps, "run_ffmpeg", counting)
    again = await _run_proxy_job(
        session_factory, redis, worker, media.id, {"profile": "PROXY_STANDARD", "nonce": 1}
    )

    assert again.status == JobStatus.SUCCEEDED
    assert calls == 0
    assert len(await _artifacts(session_factory, media.id, "PROXY")) == 1
    assert len(await _artifacts(session_factory, media.id, "THUMBNAIL")) == 1


async def test_video_without_audio(session_factory, redis, storage, worker, videos):
    media = await _probed_media(session_factory, storage, videos["noaudio"])
    job = await _run_proxy_job(session_factory, redis, worker, media.id)
    assert job.status == JobStatus.SUCCEEDED, job.error_message
    [proxy] = await _artifacts(session_factory, media.id, "PROXY")
    assert (await probe(storage.path(proxy.storage_key))).audio_codec is None


async def test_portrait_video_keeps_orientation(session_factory, redis, storage, worker, videos):
    media = await _probed_media(session_factory, storage, videos["portrait"])
    job = await _run_proxy_job(session_factory, redis, worker, media.id)
    assert job.status == JobStatus.SUCCEEDED, job.error_message
    [proxy] = await _artifacts(session_factory, media.id, "PROXY")
    meta = await probe(storage.path(proxy.storage_key))
    assert meta.width < meta.height


async def test_corrupt_source_fails_once_and_keeps_log(session_factory, redis, storage, worker, videos):
    media = await _probed_media(session_factory, storage, videos["corrupt"])
    job = await _run_proxy_job(session_factory, redis, worker, media.id)

    assert (job.status, job.error_code, job.attempt) == (JobStatus.FAILED, "CORRUPT_SOURCE", 1)
    [log] = await _artifacts(session_factory, media.id, "LOG")
    log_text = storage.path(log.storage_key).read_text()
    assert "Invalid data" in log_text or "moov atom not found" in log_text
    assert await _artifacts(session_factory, media.id, "PROXY") == []


async def test_proxy_endpoint_is_idempotent_and_requires_probe(
    settings, client_factory, session_factory, storage, videos
):
    media = await _probed_media(session_factory, storage, videos["noaudio"])
    async with client_factory(settings) as client:
        first = await client.post(f"/media/{media.id}/proxy")
        second = await client.post(f"/media/{media.id}/proxy")
        async with session_factory() as s:
            unprobed = Media(original_filename="u.mp4", storage_key="media/u/source.mp4", size_bytes=1)
            s.add(unprobed)
            await s.commit()
        blocked = await client.post(f"/media/{unprobed.id}/proxy")

    assert first.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert blocked.status_code == 409
