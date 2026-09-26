import subprocess

import pytest

from app.media.ffprobe import ProbeError, probe


def _make_video(path):
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
         "-t", "1", "-c:v", "libx264", str(path)],
        check=True,
    )


async def test_probe_real_file(tmp_path):
    src = tmp_path / "v.mp4"
    _make_video(src)
    m = await probe(src)
    assert (m.width, m.height) == (320, 240)
    assert m.video_codec == "h264"


async def test_probe_garbage_file_is_corrupt_source(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00\x01not a video" * 100)
    with pytest.raises(ProbeError) as exc:
        await probe(bad)
    assert exc.value.code == "CORRUPT_SOURCE"
