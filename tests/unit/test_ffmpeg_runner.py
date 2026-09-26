import pathlib

import pytest

from app.jobs.errors import ErrorCode
from app.media.ffmpeg import classify_ffmpeg_error, parse_progress_line
from app.media.profiles import PROFILES, scale_filter


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("out_time_us=5000000", 0.5),
        ("out_time_us=12000000", 1.0),  # clamped
        ("out_time_us=N/A", None),
        ("out_time_us=-5000", 0.0),
        ("progress=continue", None),
        ("frame=12", None),
    ],
)
def test_parse_progress_line(line, expected):
    assert parse_progress_line(line, total_seconds=10.0) == expected


def test_parse_progress_line_without_known_duration():
    assert parse_progress_line("out_time_us=5000000", total_seconds=0) is None


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("Error opening input files: Invalid data found when processing input", ErrorCode.CORRUPT_SOURCE),
        ("[mov,mp4 @ 0x1] moov atom not found", ErrorCode.CORRUPT_SOURCE),
        ("Error opening input files: No such file or directory", ErrorCode.MISSING_SOURCE),
        ("Error opening output files: Encoder not found", ErrorCode.ENCODER_FAILURE),
        ("Error while opening encoder for output stream #0:0", ErrorCode.ENCODER_FAILURE),
        ("Error initializing output stream 0:0", ErrorCode.ENCODER_FAILURE),
        ("av_malloc: Cannot allocate memory", ErrorCode.RESOURCE_EXHAUSTED),
        ("Error writing trailer: No space left on device", ErrorCode.STORAGE_ERROR),
        ("something nobody anticipated", ErrorCode.PROCESS_INTERRUPTED),
    ],
)
def test_classify_ffmpeg_error(stderr, code):
    assert classify_ffmpeg_error(stderr) is code


def test_scale_filter_never_upscales_and_keeps_even_width():
    assert scale_filter(1080) == "scale=-2:'min(1080,ih)'"


def test_profiles_are_predefined_and_named_consistently():
    assert set(PROFILES) == {
        "PROXY_STANDARD",
        "EXPORT_DEFAULT",
        "EXPORT_FALLBACK_CODEC",
        "EXPORT_LOWER_RES",
    }
    for name, p in PROFILES.items():
        assert p.name == name
        assert "-c:v" in p.video_args
    assert PROFILES["EXPORT_LOWER_RES"].max_height < PROFILES["EXPORT_DEFAULT"].max_height


def test_no_shell_execution_anywhere_in_app():
    app_dir = pathlib.Path(__file__).parents[2] / "app"
    offenders = [
        str(p)
        for p in app_dir.rglob("*.py")
        if any(s in p.read_text() for s in ("shell=True", "create_subprocess_shell", "os.system("))
    ]
    assert offenders == []
