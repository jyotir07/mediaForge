import pytest

from app.media.sniff import is_consistent, sniff_container


def ftyp(brand: bytes) -> bytes:
    return b"\x00\x00\x00\x20ftyp" + brand + b"\x00" * 52


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (ftyp(b"isom"), "mp4"),
        (ftyp(b"qt  "), "mov"),
        (b"\x1a\x45\xdf\xa3" + b"\x00" * 20 + b"matroska" + b"\x00" * 32, "matroska"),
        (b"\x1a\x45\xdf\xa3" + b"\x00" * 20 + b"webm" + b"\x00" * 36, "webm"),
        (b"RIFF\x00\x00\x00\x00AVI LIST" + b"\x00" * 48, "avi"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 56, None),
        (b"", None),
    ],
)
def test_sniff_container(head, expected):
    assert sniff_container(head) == expected


@pytest.mark.parametrize(
    ("ext", "container", "ok"),
    [
        (".mp4", "mp4", True),
        (".mp4", "mov", True),
        (".mov", "mp4", True),
        (".m4v", "mp4", True),
        (".mkv", "matroska", True),
        (".webm", "webm", True),
        (".avi", "avi", True),
        (".mp4", "matroska", False),
        (".avi", "mp4", False),
        (".mp4", None, False),
    ],
)
def test_extension_consistency(ext, container, ok):
    assert is_consistent(ext, container) is ok
