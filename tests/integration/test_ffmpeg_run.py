import pytest

from app.jobs.errors import ErrorCode, JobError
from app.media.ffmpeg import run_ffmpeg

TESTSRC = ["-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25"]


async def test_success_reports_non_decreasing_progress(tmp_path):
    seen: list[float] = []

    async def on_progress(f: float) -> None:
        seen.append(f)

    result = await run_ffmpeg(
        [*TESTSRC, "-t", "2", "-f", "null", "-"],
        total_seconds=2,
        on_progress=on_progress,
        log_path=tmp_path / "ok.log",
        timeout_s=60,
    )
    assert result.exit_code == 0
    assert result.args[0] == "ffmpeg"
    assert seen and seen == sorted(seen)
    assert seen[-1] == pytest.approx(1.0, abs=0.05)
    assert (tmp_path / "ok.log").exists()


async def test_nonzero_exit_raises_classified_error_with_bounded_tail(tmp_path):
    with pytest.raises(JobError) as exc:
        await run_ffmpeg(
            ["-i", str(tmp_path / "nope.mp4"), "-f", "null", "-"],
            total_seconds=None,
            on_progress=None,
            log_path=tmp_path / "fail.log",
            timeout_s=30,
        )
    err = exc.value
    assert err.code is ErrorCode.MISSING_SOURCE
    assert err.observation["exit_code"] != 0
    assert len(err.observation["stderr_tail"]) <= 4096
    assert "No such file" in (tmp_path / "fail.log").read_text()


async def test_timeout_kills_process(tmp_path):
    with pytest.raises(JobError) as exc:
        await run_ffmpeg(
            ["-re", *TESTSRC, "-t", "60", "-f", "null", "-"],
            total_seconds=60,
            on_progress=None,
            log_path=tmp_path / "slow.log",
            timeout_s=1,
        )
    assert exc.value.code is ErrorCode.TIMEOUT
