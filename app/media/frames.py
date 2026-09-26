from pathlib import Path

from app.media.scenes import Segment

MAX_FRAMES = 24


def pick_frame_times(segments: list[Segment], max_frames: int = MAX_FRAMES) -> list[tuple[int, float]]:
    """One representative frame (the midpoint) per segment, evenly subsampled to bound LLM cost."""
    chosen = segments
    if len(segments) > max_frames:
        n = len(segments)
        chosen = [segments[round(i * (n - 1) / (max_frames - 1))] for i in range(max_frames)]
    return [(s.index, round((s.start + s.end) / 2, 3)) for s in chosen]


def build_frame_args(src: str | Path, t: float, dst: str | Path) -> list[str]:
    # -ss before -i seeks by keyframe index instead of decoding everything up to t.
    return ["-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1", "-vf", "scale=512:-2", "-q:v", "3", str(dst)]
