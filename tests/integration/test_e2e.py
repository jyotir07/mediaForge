import os

import pytest

from app.ai.llm import FakeLLM, create_llm
from app.decision import create_decision_layer
from app.media.ffprobe import probe
from tests.fixtures.make_videos import make_scenes_video


async def drain(worker) -> None:
    while await worker.run_once():
        pass


def scene_response() -> dict:
    return {
        "overall_summary": "Three test-pattern scenes.",
        "scenes": [
            {"segment_index": 0, "summary": "Moving test pattern", "relevance": 0.3},
            {"segment_index": 1, "summary": "Colour bars, silent", "relevance": 0.5},
            {"segment_index": 2, "summary": "RGB test pattern", "relevance": 0.7},
        ],
    }


EDIT_PROPOSAL = {
    "target_duration_seconds": 8,
    "clips": [
        {"start": 0.5, "end": 3.5, "reason": "opening pattern", "source_segments": [0]},
        {"start": 4.5, "end": 7.5, "reason": "colour bars", "source_segments": [1]},
        {"start": 8.5, "end": 11.5, "reason": "rgb pattern", "source_segments": [2]},
    ],
}


async def run_pipeline(client, worker, video, request: str) -> tuple[dict, dict]:
    up = await client.post("/media", params={"filename": "demo.mp4"}, content=video.read_bytes())
    assert up.status_code == 201, up.text
    media_id = up.json()["media_id"]
    await drain(worker)
    assert (await client.get(f"/media/{media_id}/metadata")).status_code == 200

    assert (await client.post(f"/media/{media_id}/proxy")).status_code == 202
    await drain(worker)
    analyze = (await client.post(f"/media/{media_id}/analyze")).json()
    await drain(worker)
    assert (await client.get(f"/jobs/{analyze['job_id']}")).json()["status"] == "SUCCEEDED"

    edit = (await client.post(f"/media/{media_id}/edit", json={"request": request})).json()
    await drain(worker)  # edit planning, then the export job it enqueues
    edit_job = (await client.get(f"/jobs/{edit['job_id']}")).json()
    assert edit_job["status"] == "SUCCEEDED", edit_job
    export = (await client.get(f"/exports/{edit_job['result']['export_id']}")).json()
    plan = (await client.get(f"/edit-plans/{edit_job['result']['edit_plan_id']}")).json()
    return export, plan


async def test_upload_to_export_over_http(settings, client_factory, worker, storage, tmp_path):
    worker.llm = FakeLLM([scene_response(), EDIT_PROPOSAL])
    video = make_scenes_video(tmp_path / "demo.mp4")

    async with client_factory(settings) as client:
        export, plan = await run_pipeline(client, worker, video, "Make an 8 second highlight reel")
        video_bytes = await client.get(export["download_url"])

    assert export["status"] == "READY"
    # Fallback decisions (no Jev in this test): scene 2 HIGH, scene 0 MEDIUM, silent scene 1 LOW -> excluded.
    values = {(d["start"], d["end"]): d["value"] for d in plan["decisions"]}
    assert values == {(0.5, 3.5): "MEDIUM_VALUE", (4.5, 7.5): "LOW_VALUE", (8.5, 11.5): "HIGH_VALUE"}
    assert plan["accepted_operations"] == [
        {"type": "trim", "start": 0.5, "end": 3.5},
        {"type": "trim", "start": 8.5, "end": 11.5},
    ]
    assert video_bytes.status_code == 200
    out = tmp_path / "export.mp4"
    out.write_bytes(video_bytes.content)
    meta = await probe(out)
    assert meta.duration_seconds == pytest.approx(6.0, abs=0.3)
    assert meta.video_codec == "h264"
    assert (meta.width, meta.height) == (640, 360)


LIVE = (
    os.environ.get("MEDIAFORGE_LIVE") == "1"
    and bool(os.environ.get("ANTHROPIC_API_KEY"))
    and bool(os.environ.get("TYPESAFE_API_KEY"))
)


@pytest.mark.skipif(
    not LIVE, reason="set MEDIAFORGE_LIVE=1 with ANTHROPIC_API_KEY and TYPESAFE_API_KEY (spends money)"
)
async def test_live_pipeline_with_real_llm_and_jev(settings, client_factory, worker, tmp_path):
    worker.llm = create_llm(settings)
    worker.decisions = create_decision_layer(settings)
    video = make_scenes_video(tmp_path / "demo.mp4", scene_s=6)

    async with client_factory(settings) as client:
        export, plan = await run_pipeline(client, worker, video, "Make a 10 second highlight reel")

    assert export["status"] == "READY"
    assert any(d["verdict_source"] == "jev" for d in plan["decisions"])
