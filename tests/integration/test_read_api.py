import uuid

from app.models import Artifact, EditPlan, Media


async def _media_with_artifact(session_factory, storage) -> tuple[Media, Artifact]:
    mid = uuid.uuid4()
    key = f"media/{mid}/thumbs/contact.jpg"
    storage.path(key).parent.mkdir(parents=True)
    storage.path(key).write_bytes(b"\xff\xd8jpegbytes")
    async with session_factory() as s:
        m = Media(id=mid, original_filename="a.mp4", storage_key=f"media/{mid}/source.mp4", size_bytes=1)
        s.add(m)
        await s.flush()
        a = Artifact(media_id=mid, type="THUMBNAIL", storage_key=key, mime_type="image/jpeg", size_bytes=11)
        s.add(a)
        await s.commit()
    return m, a


async def test_artifact_file_is_served_with_its_mime_type(settings, client_factory, session_factory, storage):
    _, artifact = await _media_with_artifact(session_factory, storage)
    async with client_factory(settings) as client:
        ok = await client.get(f"/artifacts/{artifact.id}/file")
        missing = await client.get(f"/artifacts/{uuid.uuid4()}/file")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/jpeg"
    assert ok.content == b"\xff\xd8jpegbytes"
    assert missing.status_code == 404


async def test_edit_plan_exposes_decisions(settings, client_factory, session_factory, storage):
    media, _ = await _media_with_artifact(session_factory, storage)
    decision = {"start": 0.0, "end": 5.0, "verdict": "VALID", "verdict_source": "jev", "value": "HIGH_VALUE"}
    async with session_factory() as s:
        plan = EditPlan(
            media_id=media.id,
            request="make a reel",
            candidate_data={"target_duration_seconds": 5.0, "clips": []},
            decisions=[decision],
            accepted_operations=[{"type": "trim", "start": 0.0, "end": 5.0}],
        )
        s.add(plan)
        await s.commit()
    async with client_factory(settings) as client:
        body = (await client.get(f"/edit-plans/{plan.id}")).json()
        missing = await client.get(f"/edit-plans/{uuid.uuid4()}")
    assert body["request"] == "make a reel"
    assert body["target_duration_seconds"] == 5.0
    assert body["decisions"] == [decision]
    assert body["accepted_operations"] == [{"type": "trim", "start": 0.0, "end": 5.0}]
    assert missing.status_code == 404


async def test_ui_is_served_at_root(settings, client_factory):
    async with client_factory(settings) as client:
        page = await client.get("/")
        health = await client.get("/healthz")
    assert page.status_code == 200
    assert "MediaForge" in page.text
    assert health.status_code == 200  # API routes still win over the static mount
