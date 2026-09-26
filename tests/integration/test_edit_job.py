import json
import uuid

import pytest
from langchain_typesafe import ChoiceAnswer, ClassifierResponse, ScoreAnswer
from sqlalchemy import select

from app.ai.llm import FakeLLM
from app.decision import DecisionLayer
from app.jobs import service
from app.jobs.states import JobStatus
from app.models import Artifact, EditPlan, Export, Job, Media
from app.queue import redis_queue
from app.services.edl import load_edl
from app.workers.analysis import analysis_key
from tests.unit.test_clip_classifier import FakeJev

DURATION = 60.0


def analysis_doc(media_id, relevance=0.9, audio=0.9) -> dict:
    return {
        "version": 1,
        "media_id": str(media_id),
        "duration": DURATION,
        "overall_summary": "A talk.",
        "segments": [
            {"index": i, "start": i * 10.0, "end": i * 10.0 + 10, "frame_key": None, "summary": f"part {i}",
             "signals": {"audio_presence": audio, "llm_relevance": relevance}}
            for i in range(6)
        ],
    }  # fmt: skip


def proposal(*clips, target=25.0) -> dict:
    return {
        "target_duration_seconds": target,
        "clips": [
            {"start": s, "end": e, "reason": f"clip {s}", "source_segments": [int(s // 10)]} for s, e in clips
        ],
    }


@pytest.fixture
def media_factory(session_factory, storage):
    async def make(relevance=0.9, audio=0.9, with_analysis=True) -> Media:
        mid = uuid.uuid4()
        async with session_factory() as s:
            m = Media(
                id=mid, original_filename="talk.mp4", storage_key=f"media/{mid}/source.mp4", size_bytes=1,
                duration_seconds=DURATION, width=1280, height=720, audio_codec="aac", probe_status="done",
            )  # fmt: skip
            s.add(m)
            await s.flush()  # no ORM relationships, so insert order must be explicit for the FK
            if with_analysis:
                key = analysis_key(mid)
                p = storage.path(key)
                p.parent.mkdir(parents=True)
                p.write_text(json.dumps(analysis_doc(mid, relevance, audio)))
                s.add(Artifact(media_id=mid, type="ANALYSIS", storage_key=key, mime_type="application/json",
                               size_bytes=p.stat().st_size))  # fmt: skip
            await s.commit()
        return m

    return make


async def _run_edit(
    session_factory, redis, worker, media_id, request="Make a 25 second highlight reel"
) -> Job:
    async with session_factory() as s:
        job, _ = await service.create_or_get(s, media_id, "edit", {"request": request.lower()})
    await redis_queue.enqueue(redis, "edit", job.id)
    assert await worker.run_once()
    async with session_factory() as s:
        return await s.get_one(Job, job.id)


async def test_edit_job_produces_plan_edl_and_queued_export(
    session_factory, redis, storage, worker, media_factory
):
    media = await media_factory()
    worker.llm = FakeLLM([proposal((0, 10), (20, 30), (40, 50), (5, 12))])

    job = await _run_edit(session_factory, redis, worker, media.id)

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    async with session_factory() as s:
        plan = await s.get_one(EditPlan, uuid.UUID(job.result["edit_plan_id"]))
        export = await s.get_one(Export, uuid.UUID(job.result["export_id"]))
        export_job = await s.get_one(Job, export.job_id)
        [edl_artifact] = (
            await s.scalars(select(Artifact).where(Artifact.media_id == media.id, Artifact.type == "EDL"))
        ).all()

    edl = load_edl(storage.path(edl_artifact.storage_key).read_text())
    assert edl.duration == pytest.approx(25.0)
    assert [(o.start, o.end) for o in edl.operations] == sorted((o.start, o.end) for o in edl.operations)
    assert plan.accepted_operations == [op.model_dump() for op in edl.operations]
    # (5, 12) overlaps (0, 10) by 5 of its 7 seconds, so the fallback validator rejects it as a duplicate.
    verdicts = {(d["start"], d["end"]): d["verdict"] for d in plan.decisions}
    assert verdicts[(5.0, 12.0)] == "DUPLICATE_CONTENT"
    assert {d["verdict_source"] for d in plan.decisions} == {"fallback"}
    assert (export_job.type, export_job.status) == ("export", JobStatus.QUEUED)
    assert export_job.config == {"edit_plan_id": str(plan.id)}
    assert await redis.llen(redis_queue.QUEUES["export"]) == 1


async def test_jev_decisions_are_recorded_with_source(session_factory, redis, storage, worker, media_factory):
    both = ClassifierResponse(
        model="jev-latest",
        answers={
            "verdict": ChoiceAnswer(
                type="choice", choice="VALID", probabilities={"VALID": 0.95}, confidence=0.9
            ),
            "value": ScoreAnswer(type="score", score=1.9, legend={0: "l", 1: "m", 2: "h"},
                                 probabilities={0: 0.0, 1: 0.1, 2: 0.9}, confidence=0.85),
        },
    )  # fmt: skip
    worker.decisions = DecisionLayer.with_jev(FakeJev(both))
    worker.llm = FakeLLM([proposal((0, 10), (20, 30))])
    media = await media_factory()

    job = await _run_edit(session_factory, redis, worker, media.id)

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    async with session_factory() as s:
        plan = await s.get_one(EditPlan, uuid.UUID(job.result["edit_plan_id"]))
    assert {(d["verdict_source"], d["value"], d["value_source"]) for d in plan.decisions} == {
        ("jev", "HIGH_VALUE", "jev")
    }


async def test_out_of_range_llm_clips_are_rejected_by_rules(
    session_factory, redis, storage, worker, media_factory
):
    worker.llm = FakeLLM([proposal((50, 90), (-5, 5), (10, 20))])
    media = await media_factory()

    job = await _run_edit(session_factory, redis, worker, media.id)

    assert job.status == JobStatus.SUCCEEDED, job.error_message
    async with session_factory() as s:
        plan = await s.get_one(EditPlan, uuid.UUID(job.result["edit_plan_id"]))
    verdicts = {(d["start"], d["end"]): (d["verdict"], d["verdict_source"]) for d in plan.decisions}
    assert verdicts[(50.0, 90.0)] == ("INVALID_RANGE", "rules")
    assert verdicts[(-5.0, 5.0)] == ("INVALID_RANGE", "rules")
    assert plan.accepted_operations == [{"type": "trim", "start": 10.0, "end": 20.0}]


async def test_all_clips_rejected_fails_clearly(session_factory, redis, storage, worker, media_factory):
    worker.llm = FakeLLM([proposal((0, 10), (20, 30))])
    media = await media_factory(relevance=0.05, audio=0.1)  # fallback scores these LOW_VALUE

    job = await _run_edit(session_factory, redis, worker, media.id)

    assert (job.status, job.error_code, job.attempt) == (JobStatus.FAILED, "NO_CLIPS_SELECTED", 1)


async def test_retry_after_plan_was_saved_reuses_it(session_factory, redis, storage, worker, media_factory):
    worker.llm = FakeLLM([proposal((0, 10), (20, 30))])
    media = await media_factory()
    job = await _run_edit(session_factory, redis, worker, media.id)

    # Simulate the job being retried after the plan was committed: the handler must not plan again.
    worker.llm = FakeLLM([])
    async with session_factory() as s:
        j = await s.get_one(Job, job.id)
        j.status, j.worker_id = JobStatus.RETRYING, None
        await s.commit()
    await redis_queue.enqueue(redis, "edit", job.id)
    assert await worker.run_once()

    async with session_factory() as s:
        again = await s.get_one(Job, job.id)
        plans = (await s.scalars(select(EditPlan).where(EditPlan.media_id == media.id))).all()
        exports = (await s.scalars(select(Export).where(Export.media_id == media.id))).all()
    assert again.status == JobStatus.SUCCEEDED
    assert (len(plans), len(exports)) == (1, 1)


async def test_edit_endpoint(settings, client_factory, media_factory):
    ready = await media_factory()
    not_analyzed = await media_factory(with_analysis=False)
    async with client_factory(settings) as client:
        a = await client.post(f"/media/{ready.id}/edit", json={"request": "Make a 45 second highlight reel"})
        b = await client.post(
            f"/media/{ready.id}/edit", json={"request": "  make a 45 second   HIGHLIGHT reel "}
        )
        blocked = await client.post(f"/media/{not_analyzed.id}/edit", json={"request": "anything"})
        empty = await client.post(f"/media/{ready.id}/edit", json={"request": ""})
    assert a.status_code == 202
    assert a.json()["job_id"] == b.json()["job_id"]
    assert blocked.status_code == 409
    assert empty.status_code == 422
