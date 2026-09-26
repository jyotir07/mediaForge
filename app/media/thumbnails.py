from pathlib import Path


def build_thumbnail_args(src: Path, dst: Path, duration: float, count: int = 9) -> list[str]:
    """A 3x3 contact sheet: sample `count` frames evenly across the video and tile them into one JPEG."""
    duration = max(duration, 1.0)
    return [
        "-i", str(src),
        "-vf", f"fps={count}/{duration:.3f},scale=320:-2,tile=3x3",
        "-frames:v", "1",
        "-q:v", "3",
        str(dst),
    ]  # fmt: skip
