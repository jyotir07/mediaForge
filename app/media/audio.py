import asyncio
import re
from pathlib import Path

from app.media.ffmpeg import run_ffmpeg
from app.media.scenes import Segment

_START = re.compile(r"silence_start: (-?\d+(?:\.\d+)?)")
_END = re.compile(r"silence_end: (-?\d+(?:\.\d+)?)")


def build_silencedetect_args(src: str | Path) -> list[str]:
    return ["-i", str(src), "-vn", "-af", "silencedetect=n=-35dB:d=0.5", "-f", "null", "-"]


def parse_silences(stderr: str, duration: float) -> list[tuple[float, float]]:
    silences: list[tuple[float, float]] = []
    open_start: float | None = None
    for line in stderr.splitlines():
        if m := _START.search(line):
            open_start = max(0.0, float(m.group(1)))
        elif m := _END.search(line):
            silences.append((open_start or 0.0, float(m.group(1))))
            open_start = None
    if open_start is not None:  # silence ran to the end of the file
        silences.append((open_start, duration))
    return silences


def audio_presence(segment: Segment, silences: list[tuple[float, float]]) -> float:
    """Fraction of the segment that is not silent (1.0 = sound throughout)."""
    if segment.duration <= 0:
        return 0.0
    silent = sum(max(0.0, min(segment.end, e) - max(segment.start, s)) for s, e in silences)
    return max(0.0, min(1.0, 1 - silent / segment.duration))


async def detect_silences(
    src: Path, duration: float, *, log_path: Path, timeout_s: float
) -> list[tuple[float, float]]:
    await run_ffmpeg(
        build_silencedetect_args(src),
        total_seconds=None,
        on_progress=None,
        log_path=log_path,
        timeout_s=timeout_s,
    )
    return parse_silences(await asyncio.to_thread(log_path.read_text, errors="replace"), duration)
