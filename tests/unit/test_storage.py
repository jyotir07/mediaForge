import uuid

import pytest

from app.storage.local import InvalidStorageKey, Storage, TooLarge, media_key


async def _chunks(*parts: bytes):
    for p in parts:
        yield p


@pytest.fixture
def storage(tmp_path) -> Storage:
    return Storage(tmp_path / "root")


@pytest.mark.parametrize(
    "key",
    ["../etc/passwd", "media/../../x", "/abs/path", "a\\b", "a\x00b", "", "media//x", "media/./x", "a b"],
)
def test_rejects_unsafe_keys(storage, key):
    with pytest.raises(InvalidStorageKey):
        storage.path(key)


def test_resolved_path_is_under_root(storage, tmp_path):
    p = storage.path("media/abc/source.mp4")
    assert p.is_relative_to((tmp_path / "root").resolve())


def test_media_key_layout():
    mid = uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert media_key(mid, "proxy", "standard.mp4") == f"media/{mid}/proxy/standard.mp4"


async def test_write_stream_writes_and_reports_size(storage):
    size = await storage.write_stream("media/a/source.mp4", _chunks(b"abc", b"def"), max_bytes=100)
    assert size == 6
    assert storage.path("media/a/source.mp4").read_bytes() == b"abcdef"


async def test_write_stream_over_limit_raises_and_leaves_no_files(storage, tmp_path):
    with pytest.raises(TooLarge):
        await storage.write_stream("media/a/source.mp4", _chunks(b"x" * 60, b"x" * 60), max_bytes=100)

    assert not storage.exists("media/a/source.mp4")
    leftovers = [p for p in (tmp_path / "root").rglob("*") if p.is_file()]
    assert leftovers == []


def test_final_file_absent_until_commit(storage):
    tmp = storage.tmp_path("media/a/proxy.mp4")
    tmp.write_bytes(b"partial")
    assert not storage.exists("media/a/proxy.mp4")

    storage.commit(tmp, "media/a/proxy.mp4")
    assert storage.exists("media/a/proxy.mp4")
    assert not tmp.exists()


def test_delete_is_idempotent(storage):
    storage.path("media/a/x.bin").parent.mkdir(parents=True)
    storage.path("media/a/x.bin").write_bytes(b"1")
    storage.delete("media/a/x.bin")
    storage.delete("media/a/x.bin")
    assert not storage.exists("media/a/x.bin")
