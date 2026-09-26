import pytest

from app.media.audio import audio_presence, build_silencedetect_args, parse_silences
from app.media.scenes import Segment

SILENCE = """\
[silencedetect @ 0x7a07380047c0] silence_start: 5.01551
[silencedetect @ 0x7a07380047c0] silence_end: 7.012426 | silence_duration: 1.996916
"""


def test_parse_silences():
    assert parse_silences(SILENCE, duration=12.0) == [(5.01551, 7.012426)]


def test_silence_running_to_end_of_file_is_closed_at_duration():
    stderr = SILENCE + "[silencedetect @ 0x1] silence_start: 10.5\n"
    assert parse_silences(stderr, duration=12.0) == [(5.01551, 7.012426), (10.5, 12.0)]


@pytest.mark.parametrize(
    ("silences", "expected"),
    [
        ([], 1.0),
        ([(5.0, 7.0)], 0.5),
        ([(0.0, 20.0)], 0.0),
        ([(1.0, 3.0)], 1.0),  # silence outside the segment
    ],
)
def test_audio_presence(silences, expected):
    assert audio_presence(Segment(0, 4.0, 8.0), silences) == pytest.approx(expected)


def test_silencedetect_args_drop_video():
    args = build_silencedetect_args("/in.mp4")
    assert "-vn" in args
    assert args[args.index("-af") + 1] == "silencedetect=n=-35dB:d=0.5"
