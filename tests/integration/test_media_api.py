import uuid

import pytest

from tests.fixtures.make_videos import make_video


@pytest.fixture(scope="session")
def sample_mp4(tmp_path_factory) -> bytes:
    return make_video(tmp_path_factory.mktemp("videos") / "sample.mp4").read_bytes()


def _stored_files(settings):
    return [p for p in settings.storage_root.rglob("*") if p.is_file()]


async def test_upload_persists_source_and_records(settings, client_factory, sample_mp4):
    async with client_factory(settings) as client:
        resp = await client.post("/media", params={"filename": "demo.mp4"}, content=sample_mp4)
        assert resp.status_code == 201, resp.text
        media_id = resp.json()["media_id"]

        got = await client.get(f"/media/{media_id}")

    assert got.status_code == 200
    body = got.json()
    assert body["original_filename"] == "demo.mp4"
    assert body["size_bytes"] == len(sample_mp4)
    [source] = [a for a in body["artifacts"] if a["type"] == "SOURCE"]
    assert source["mime_type"] == "video/mp4"
    stored = settings.storage_root / source["storage_key"]
    assert stored.read_bytes() == sample_mp4


async def test_extension_not_trusted_png_bytes_rejected(settings, client_factory):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000
    async with client_factory(settings) as client:
        resp = await client.post("/media", params={"filename": "evil.mp4"}, content=png)
    assert resp.status_code == 415
    assert _stored_files(settings) == []


async def test_unsupported_extension_rejected(settings, client_factory, sample_mp4):
    async with client_factory(settings) as client:
        resp = await client.post("/media", params={"filename": "notes.txt"}, content=sample_mp4)
    assert resp.status_code == 415


async def test_oversized_chunked_upload_rejected_without_leftovers(settings, client_factory, sample_mp4):
    settings.max_upload_bytes = 1024

    async def body():
        for i in range(0, len(sample_mp4), 512):
            yield sample_mp4[i : i + 512]

    async with client_factory(settings) as client:
        resp = await client.post("/media", params={"filename": "big.mp4"}, content=body())
    assert resp.status_code == 413
    assert _stored_files(settings) == []


async def test_unknown_media_is_404(settings, client_factory):
    async with client_factory(settings) as client:
        assert (await client.get(f"/media/{uuid.uuid4()}")).status_code == 404
        assert (await client.get(f"/media/{uuid.uuid4()}/metadata")).status_code == 404


async def test_metadata_is_409_before_probe(settings, client_factory, sample_mp4):
    async with client_factory(settings) as client:
        media_id = (await client.post("/media", params={"filename": "a.mp4"}, content=sample_mp4)).json()[
            "media_id"
        ]
        resp = await client.get(f"/media/{media_id}/metadata")
    assert resp.status_code == 409
