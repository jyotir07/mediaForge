import subprocess
from pathlib import Path


def make_video(path: Path, *, seconds: float = 2, size: str = "320x240", audio: bool = True) -> Path:
    args = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440"]
    args += ["-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    subprocess.run([*args, str(path)], check=True)
    return path
