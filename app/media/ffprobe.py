import asyncio
import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any


class ProbeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MediaMetadata:
    duration_seconds: float
    width: int
    height: int
    fps: float
    rotation: int
    video_codec: str
    audio_codec: str | None
    container: str
    bitrate: int | None
    size_bytes: int


def _fps(stream: dict[str, Any]) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key, "0/0")
        num, _, den = raw.partition("/")
        if den and int(den) != 0 and int(num) != 0:
            return float(Fraction(int(num), int(den)))
    raise ProbeError("CORRUPT_SOURCE", "video stream has no usable frame rate")


def _rotation(stream: dict[str, Any]) -> int:
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            return int(side["rotation"])
    return int(stream.get("tags", {}).get("rotate", 0))


def _container(fmt: dict[str, Any]) -> str:
    names = fmt.get("format_name", "").split(",")
    if names[0] == "mov":
        # ffprobe reports the whole ISO-BMFF family as "mov,mp4,..."; the brand tells them apart.
        brand = fmt.get("tags", {}).get("major_brand", "").strip()
        return "mov" if brand == "qt" else "mp4"
    return names[0]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_ffprobe(raw: dict[str, Any]) -> MediaMetadata:
    streams = raw.get("streams", [])
    fmt = raw.get("format", {})
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        raise ProbeError("NO_VIDEO_STREAM", "no video stream found")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = float(fmt.get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise ProbeError("CORRUPT_SOURCE", "media has no duration")

    rotation = _rotation(video)
    width, height = int(video["width"]), int(video["height"])
    if abs(rotation) % 180 == 90:
        width, height = height, width

    return MediaMetadata(
        duration_seconds=duration,
        width=width,
        height=height,
        fps=_fps(video),
        rotation=rotation,
        video_codec=video["codec_name"],
        audio_codec=audio["codec_name"] if audio else None,
        container=_container(fmt),
        bitrate=_int_or_none(fmt.get("bit_rate")),
        size_bytes=int(fmt.get("size", 0)),
    )


async def probe(path: Path, timeout_s: float = 30) -> MediaMetadata:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise ProbeError("CORRUPT_SOURCE", stderr.decode(errors="replace")[-2000:].strip())
    return parse_ffprobe(json.loads(stdout))
