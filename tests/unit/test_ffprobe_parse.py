import json
from pathlib import Path

import pytest

from app.media.ffprobe import ProbeError, parse_ffprobe

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ffprobe"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_parses_h264_aac_mp4():
    m = parse_ffprobe(load("h264_aac"))
    assert (m.width, m.height) == (1280, 720)
    assert m.fps == pytest.approx(30.0)
    assert m.video_codec == "h264"
    assert m.audio_codec == "aac"
    assert m.container == "mp4"
    assert m.duration_seconds == pytest.approx(2.0, abs=0.1)
    assert m.rotation == 0
    assert m.bitrate is not None and m.bitrate > 0
    assert m.size_bytes > 0


def test_video_only_has_no_audio_codec():
    m = parse_ffprobe(load("video_only"))
    assert m.audio_codec is None
    assert m.fps == pytest.approx(25.0)


def test_ntsc_fractional_frame_rate():
    assert parse_ffprobe(load("ntsc")).fps == pytest.approx(29.97, abs=0.01)


def test_rotation_swaps_to_display_dimensions():
    m = parse_ffprobe(load("rotated"))
    assert m.rotation == 90
    assert (m.width, m.height) == (1080, 1920)
    assert m.container == "mov"


def test_matroska_container_name():
    assert parse_ffprobe(load("na_bitrate")).container == "matroska"


@pytest.mark.parametrize("value", ["N/A", None])
def test_missing_bitrate_is_none(value):
    raw = load("na_bitrate")
    if value is None:
        raw["format"].pop("bit_rate", None)
    else:
        raw["format"]["bit_rate"] = value
    for s in raw["streams"]:
        s.pop("bit_rate", None)
    assert parse_ffprobe(raw).bitrate is None


def test_audio_only_file_is_rejected():
    with pytest.raises(ProbeError) as exc:
        parse_ffprobe(load("audio_only"))
    assert exc.value.code == "NO_VIDEO_STREAM"


def test_cover_art_stream_is_not_treated_as_video():
    raw = load("audio_only")
    raw["streams"].append(
        {
            "codec_type": "video",
            "codec_name": "mjpeg",
            "width": 300,
            "height": 300,
            "avg_frame_rate": "0/0",
            "disposition": {"attached_pic": 1},
        }
    )
    with pytest.raises(ProbeError):
        parse_ffprobe(raw)
