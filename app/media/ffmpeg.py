import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.jobs.errors import ErrorCode, JobError

STDERR_TAIL_BYTES = 4096
PREFIX = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"]

ProgressCallback = Callable[[float], Awaitable[None]]

# Ordered: the first matching signature wins.
_ERROR_SIGNATURES: list[tuple[tuple[str, ...], ErrorCode]] = [
    (("No space left on device",), ErrorCode.STORAGE_ERROR),
    (("Cannot allocate memory", "Out of memory"), ErrorCode.RESOURCE_EXHAUSTED),
    (("No such file or directory",), ErrorCode.MISSING_SOURCE),
    (
        (
            "Invalid data found when processing input",
            "moov atom not found",
            "could not find codec parameters",
        ),
        ErrorCode.CORRUPT_SOURCE,
    ),
    (
        (
            "Encoder not found",
            "Unknown encoder",
            "Error while opening encoder",
            "Error initializing output stream",
            "Could not open encoder",
        ),
        ErrorCode.ENCODER_FAILURE,
    ),
]


@dataclass(frozen=True)
class FfmpegResult:
    args: list[str]
    exit_code: int
    duration_ms: int
    stderr_tail: str
    log_path: Path


def parse_progress_line(line: str, total_seconds: float | None) -> float | None:
    key, _, value = line.strip().partition("=")
    if key != "out_time_us" or not total_seconds or total_seconds <= 0:
        return None
    try:
        seconds = int(value) / 1_000_000
    except ValueError:
        return None
    return max(0.0, min(1.0, seconds / total_seconds))


def classify_ffmpeg_error(stderr: str) -> ErrorCode:
    for needles, code in _ERROR_SIGNATURES:
        if any(n in stderr for n in needles):
            return code
    return ErrorCode.PROCESS_INTERRUPTED


async def run_ffmpeg(
    args: list[str],
    *,
    total_seconds: float | None,
    on_progress: ProgressCallback | None,
    log_path: Path,
    timeout_s: float,
) -> FfmpegResult:
    """Run ffmpeg with an argument array (never a shell), streaming progress and the full stderr log to
    log_path while keeping only a bounded tail in memory."""
    full_args = [*PREFIX, *args]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *full_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = proc.stdout, proc.stderr
    if stdout is None or stderr is None:
        raise RuntimeError("ffmpeg pipes were not opened")
    tail = bytearray()

    async def read_progress() -> None:
        last = -1.0
        async for raw in stdout:
            fraction = parse_progress_line(raw.decode(errors="replace"), total_seconds)
            if fraction is not None and fraction > last and on_progress is not None:
                last = fraction
                await on_progress(fraction)

    async def read_stderr() -> None:
        with log_path.open("wb") as log:
            async for raw in stderr:
                log.write(raw)
                tail.extend(raw)
                del tail[:-STDERR_TAIL_BYTES]

    try:
        async with asyncio.timeout(timeout_s):
            await asyncio.gather(read_progress(), read_stderr())
            exit_code = await proc.wait()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise JobError(
            ErrorCode.TIMEOUT, f"ffmpeg exceeded {timeout_s}s", observation={"args": full_args}
        ) from None
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise

    stderr_tail = tail.decode(errors="replace")
    result = FfmpegResult(
        args=full_args,
        exit_code=exit_code,
        duration_ms=int((time.monotonic() - started) * 1000),
        stderr_tail=stderr_tail,
        log_path=log_path,
    )
    if exit_code != 0:
        raise JobError(
            classify_ffmpeg_error(stderr_tail),
            stderr_tail.strip().splitlines()[-1] if stderr_tail.strip() else f"ffmpeg exited {exit_code}",
            observation={"args": full_args, "exit_code": exit_code, "stderr_tail": stderr_tail},
        )
    return result
