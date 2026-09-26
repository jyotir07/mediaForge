import asyncio
import math
import re
from dataclasses import dataclass
from pathlib import Path

from app.media.ffmpeg import run_ffmpeg

_PTS_TIME = re.compile(r"pts_time:(\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class Segment:
    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def build_scene_detect_args(src: str | Path, threshold: float = 0.3) -> list[str]:
    # The comma inside gt() must be escaped or ffmpeg reads it as a filter separator.
    return ["-i", str(src), "-vf", f"select=gt(scene\\,{threshold}),showinfo", "-an", "-f", "null", "-"]


def parse_scene_times(stderr: str) -> list[float]:
    return [
        float(m.group(1))
        for line in stderr.splitlines()
        if "Parsed_showinfo" in line and (m := _PTS_TIME.search(line))
    ]


def _merge_slivers(spans: list[list[float]], min_len: float) -> None:
    while len(spans) > 1:
        short = next((k for k, (a, b) in enumerate(spans) if b - a < min_len), None)
        if short is None:
            return
        if short == 0:
            spans[1][0] = spans[0][0]
        elif short == len(spans) - 1:
            spans[-2][1] = spans[-1][1]
        else:
            prev_len = spans[short - 1][1] - spans[short - 1][0]
            next_len = spans[short + 1][1] - spans[short + 1][0]
            if prev_len <= next_len:
                spans[short - 1][1] = spans[short][1]
            else:
                spans[short + 1][0] = spans[short][0]
        del spans[short]


def build_segments(
    cuts: list[float], duration: float, min_len: float = 2.0, max_len: float = 15.0, window: float = 8.0
) -> list[Segment]:
    """Turn detected cut points into contiguous segments covering [0, duration]. With no usable cuts
    (a single long shot), fall back to fixed windows, so there is always at least one candidate."""
    if duration <= 0:
        raise ValueError("duration must be positive")
    points = sorted({c for c in cuts if 0 < c < duration})
    if not points:
        n = max(1, math.ceil(duration / window - 1e-9))
        points = [window * i for i in range(1, n)]

    bounds = [0.0, *points, duration]
    spans = [[a, b] for a, b in zip(bounds, bounds[1:], strict=False)]
    _merge_slivers(spans, min_len)

    edges = [0.0]
    for a, b in spans:
        parts = max(1, math.ceil((b - a) / max_len - 1e-9))
        step = (b - a) / parts
        edges += [a + step * j for j in range(1, parts)] + [b]
    edges = [round(e, 3) for e in edges]
    return [Segment(i, a, b) for i, (a, b) in enumerate(zip(edges, edges[1:], strict=False))]


async def detect_scene_cuts(
    src: Path, *, log_path: Path, timeout_s: float, threshold: float = 0.3
) -> list[float]:
    await run_ffmpeg(
        build_scene_detect_args(src, threshold),
        total_seconds=None,
        on_progress=None,
        log_path=log_path,
        timeout_s=timeout_s,
    )
    # Parse the full log, not the in-memory tail: long videos produce many showinfo lines.
    return parse_scene_times(await asyncio.to_thread(log_path.read_text, errors="replace"))
