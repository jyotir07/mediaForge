from typing import Literal

Container = Literal["mp4", "mov", "matroska", "webm", "avi"]

SNIFF_BYTES = 64

# ISO-BMFF files are routinely named .mp4/.mov/.m4v interchangeably, and .webm is a Matroska profile.
_ALLOWED: dict[str, set[str]] = {
    ".mp4": {"mp4", "mov"},
    ".m4v": {"mp4", "mov"},
    ".mov": {"mov", "mp4"},
    ".mkv": {"matroska", "webm"},
    ".webm": {"webm", "matroska"},
    ".avi": {"avi"},
}

ALLOWED_EXTENSIONS = frozenset(_ALLOWED)

MIME_TYPES: dict[str, str] = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "matroska": "video/x-matroska",
    "webm": "video/webm",
    "avi": "video/x-msvideo",
}


def sniff_container(head: bytes) -> Container | None:
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mov" if head[8:12] == b"qt  " else "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm" if b"webm" in head[:SNIFF_BYTES] else "matroska"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "avi"
    return None


def is_consistent(ext: str, container: str | None) -> bool:
    return container is not None and container in _ALLOWED.get(ext, set())
