import pytest

from app.media.audio import detect_silences
from app.media.scenes import detect_scene_cuts
from tests.fixtures.make_videos import make_scenes_video


@pytest.fixture(scope="module")
def scenes_video(tmp_path_factory):
    return make_scenes_video(tmp_path_factory.mktemp("det") / "scenes.mp4")


async def test_detects_hard_cuts_in_real_video(scenes_video, tmp_path):
    cuts = await detect_scene_cuts(scenes_video, log_path=tmp_path / "scenes.log", timeout_s=60)
    assert cuts == pytest.approx([4.0, 8.0], abs=0.1)


async def test_detects_silent_middle_scene(scenes_video, tmp_path):
    silences = await detect_silences(scenes_video, 12.0, log_path=tmp_path / "silence.log", timeout_s=60)
    assert len(silences) == 1
    start, end = silences[0]
    assert start == pytest.approx(4.0, abs=0.2)
    assert end == pytest.approx(8.0, abs=0.2)
