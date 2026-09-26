import subprocess
from pathlib import Path

_FAST = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def make_video(path: Path, *, seconds: float = 2, size: str = "320x240", audio: bool = True) -> Path:
    args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440"]
    args += ["-t", str(seconds), *_FAST]
    args += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    _ffmpeg(*args, str(path))
    return path


def make_scenes_video(path: Path, *, size: str = "640x360", scene_s: int = 4) -> Path:
    """Three distinct scenes, hard cuts at scene_s and 2*scene_s; the middle scene is silent."""
    total = 3 * scene_s
    sources = ("testsrc2", "smptebars", "rgbtestsrc")
    inputs = [
        a for src in sources for a in ("-f", "lavfi", "-i", f"{src}=size={size}:rate=25:duration={scene_s}")
    ]
    silent = f"between(t\\,{scene_s}\\,{2 * scene_s})"
    _ffmpeg(
        *inputs,
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={total}",
        "-filter_complex", f"[0:v][1:v][2:v]concat=n=3:v=1:a=0[v];[3:a]volume=enable='{silent}':volume=0[a]",
        "-map", "[v]", "-map", "[a]", *_FAST, "-c:a", "aac", str(path),
    )  # fmt: skip
    return path


def make_corrupt_video(path: Path, source: Path) -> Path:
    # x264 in mp4 writes the moov atom last, so a truncated file has no index and cannot be decoded.
    data = source.read_bytes()
    path.write_bytes(data[: len(data) // 3])
    return path
