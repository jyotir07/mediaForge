import json
import uuid

import pytest
from sqlalchemy import select

from app.ai.llm import FakeLLM, ImagePart
from app.jobs import service
from app.jobs.states import JobStatus
from app.media.ffprobe import probe
from app.models import Artifact, Job, Media
from app.queue import redis_queue
from app.workers import steps
from tests.fixtures.make_videos import make_scenes_video, make_video


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    d = tmp_path_factory.mktemp("analysis_videos")
    return {
        "scenes": make_scenes_video(d / "scenes.mp4"),
        "noaudio": make_video(d / "noaudio.mp4", seconds=3, audio=False),
    }


def scene_response(n: int) -> dict:
    return {
        "overall_summary": "Test pattern footage.",
        "scenes": [
            {"segment_index": i, "summary": f"scene {i}", "relevance": 0.3 + 0.2 * i} for i in range(n)
        ],
    }


async def _probed_media(session_factory, storage, src) -> Media:
    mid = uuid.uuid4()
    key = f"media/{mid}/source.mp4"
    p = storage.path(key)
    p.parent.mkdir(parents=True)
    p.write_bytes(src.read_bytes())
    meta = await probe(p)
    async with session_factory() as s:
        m = Media(
            id=mid,
            original_filename="x.mp4",
            storage_key=key,
            size_bytes=p.stat().st_size,
            duration_seconds=meta.duration_seconds,
            width=meta.width,
            height=meta.height,
            audio_codec=meta.audio_codec,
            probe_status="done",
        )
        s.add(m)
        await s.commit()
    return m


async def _run_analyze(session_factory, redis, worker, media_id) -> Job:
    async with session_factory() as s:
        job, _ = await service.create_or_get(s, media_id, "analyze", {"version": 1})
    await redis_queue.enqueue(redis, "analyze", job.id)
    assert await worker.run_once()
    async with session_factory() as s:
        return await s.get_one(Job, job.id)


async def _analysis(session_factory, storage, media_id) -> dict:
    async with session_factory() as s:
        [a] = (
            await s.scalars(
                select(Artifact).where(Artifact.media_id == media_id, Artifact.type == "ANALYSIS")
            )
        ).all()
    return json.loads(storage.path(a.storage_key).read_text())


async def test_analysis_produces_segments_frames_and_signals(session_factory, redis, storage, worker, videos):
    worker.llm = FakeLLM([scene_response(3)])
    media = await _probed_media(session_factory, storage, videos["scenes"])

    job = await _run_analyze(session_factory, redis, worker, media.id)

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    analysis = await _analysis(session_factory, storage, media.id)
    segs = analysis["segments"]
    assert [round(s["start"]) for s in segs] == [0, 4, 8]
    for s in segs:
        assert storage.exists(s["frame_key"])
        assert 0.0 <= s["signals"]["audio_presence"] <= 1.0
    assert segs[1]["signals"]["audio_presence"] < 0.2  # the silent middle scene
    assert segs[0]["signals"]["audio_presence"] > 0.8
    assert segs[2]["signals"]["llm_relevance"] == pytest.approx(0.7)
    assert segs[0]["summary"] == "scene 0"
    assert analysis["overall_summary"] == "Test pattern footage."

    [call] = worker.llm.calls
    assert sum(isinstance(p, ImagePart) for p in call["parts"]) == 3


async def test_video_without_audio(session_factory, redis, storage, worker, videos):
    worker.llm = FakeLLM([scene_response(1)])
    media = await _probed_media(session_factory, storage, videos["noaudio"])

    job = await _run_analyze(session_factory, redis, worker, media.id)

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    segs = (await _analysis(session_factory, storage, media.id))["segments"]
    assert len(segs) == 1
    assert segs[0]["signals"]["audio_presence"] == 0.0


async def test_segments_missing_from_llm_answer_get_zero_relevance(
    session_factory, redis, storage, worker, videos
):
    worker.llm = FakeLLM([scene_response(1)])  # only describes segment 0 of 3
    media = await _probed_media(session_factory, storage, videos["scenes"])

    job = await _run_analyze(session_factory, redis, worker, media.id)

    assert job.status == JobStatus.SUCCEEDED
    segs = (await _analysis(session_factory, storage, media.id))["segments"]
    assert [s["signals"]["llm_relevance"] for s in segs] == [pytest.approx(0.3), 0.0, 0.0]


async def test_malformed_llm_output_fails_without_retry(session_factory, redis, storage, worker, videos):
    worker.llm = FakeLLM(["garbage", '{"scenes": "nope"}'])
    media = await _probed_media(session_factory, storage, videos["scenes"])

    job = await _run_analyze(session_factory, redis, worker, media.id)

    assert (job.status, job.error_code, job.attempt) == (JobStatus.FAILED, "LLM_OUTPUT_INVALID", 1)


async def test_rerun_reuses_analysis_without_ffmpeg_or_llm(
    session_factory, redis, storage, worker, videos, monkeypatch
):
    worker.llm = FakeLLM([scene_response(3)])
    media = await _probed_media(session_factory, storage, videos["scenes"])
    await _run_analyze(session_factory, redis, worker, media.id)

    async def no_ffmpeg(*a, **kw):
        raise AssertionError("ffmpeg should not run")

    monkeypatch.setattr(steps, "run_ffmpeg", no_ffmpeg)
    worker.llm = FakeLLM([])  # any call would raise
    async with session_factory() as s:
        job, _ = await service.create_or_get(s, media.id, "analyze", {"version": 1, "nonce": 1})
    await redis_queue.enqueue(redis, "analyze", job.id)
    assert await worker.run_once()
    async with session_factory() as s:
        assert (await s.get_one(Job, job.id)).status == JobStatus.SUCCEEDED


async def test_analyze_endpoint_requires_probe_and_is_idempotent(
    settings, client_factory, session_factory, storage, videos
):
    media = await _probed_media(session_factory, storage, videos["noaudio"])
    async with session_factory() as s:
        unprobed = Media(original_filename="u.mp4", storage_key="media/u/source.mp4", size_bytes=1)
        s.add(unprobed)
        await s.commit()
    async with client_factory(settings) as client:
        a = await client.post(f"/media/{media.id}/analyze")
        b = await client.post(f"/media/{media.id}/analyze")
        blocked = await client.post(f"/media/{unprobed.id}/analyze")
    assert a.status_code == 202
    assert a.json()["job_id"] == b.json()["job_id"]
    assert blocked.status_code == 409
