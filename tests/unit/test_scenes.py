import pytest

from app.media.frames import build_frame_args, pick_frame_times
from app.media.scenes import Segment, build_scene_detect_args, build_segments, parse_scene_times

SHOWINFO = """\
[Parsed_showinfo_1 @ 0x753b74004a40] n:   0 pts:  51200 pts_time:4       duration:    512 duration_time:0.04
[Parsed_showinfo_1 @ 0x753b74004a40] n:   1 pts: 102400 pts_time:8       duration:    512 duration_time:0.04
[Parsed_showinfo_1 @ 0x753b74004a40] n:   2 pts: 102400 pts_time:9.52    duration:    512 duration_time:0.04
"""


def _spans(segments):
    return [(s.start, s.end) for s in segments]


def _assert_partition(segments, duration):
    assert segments[0].start == 0.0
    assert segments[-1].end == pytest.approx(duration)
    for a, b in zip(segments, segments[1:], strict=False):
        assert a.end == b.start
    assert [s.index for s in segments] == list(range(len(segments)))


def test_parse_scene_times_from_showinfo():
    assert parse_scene_times(SHOWINFO) == [4.0, 8.0, 9.52]


def test_scene_detect_args_escape_filter_comma():
    args = build_scene_detect_args("/in.mp4", threshold=0.3)
    assert args[args.index("-vf") + 1] == "select=gt(scene\\,0.3),showinfo"
    assert args[-3:] == ["-f", "null", "-"]


def test_cuts_become_contiguous_segments():
    segs = build_segments([4.0, 8.0], 12.0)
    assert _spans(segs) == [(0.0, 4.0), (4.0, 8.0), (8.0, 12.0)]
    _assert_partition(segs, 12.0)


def test_short_video_without_cuts_is_one_segment():
    assert _spans(build_segments([], 8.0)) == [(0.0, 8.0)]


def test_no_cuts_on_long_video_falls_back_to_fixed_windows():
    segs = build_segments([], 40.0, window=8.0)
    assert _spans(segs) == [(0.0, 8.0), (8.0, 16.0), (16.0, 24.0), (24.0, 32.0), (32.0, 40.0)]


def test_slivers_merge_into_neighbours():
    assert _spans(build_segments([0.5, 6.0], 12.0)) == [(0.0, 6.0), (6.0, 12.0)]
    assert _spans(build_segments([11.8], 12.0)) == [(0.0, 12.0)]


def test_long_segments_are_split_evenly():
    segs = build_segments([2.0], 40.0, max_len=15.0)
    # (2, 40) is 38s long, so it becomes three equal ~12.67s parts.
    assert _spans(segs) == [(0.0, 2.0), (2.0, 14.667), (14.667, 27.333), (27.333, 40.0)]
    _assert_partition(segs, 40.0)


def test_garbage_cuts_are_ignored():
    assert _spans(build_segments([8.0, 4.0, 4.0, -1.0, 13.0, 0.0, 12.0], 12.0)) == [
        (0.0, 4.0),
        (4.0, 8.0),
        (8.0, 12.0),
    ]


def test_video_shorter_than_min_len_is_still_one_segment():
    assert _spans(build_segments([], 1.2)) == [(0.0, 1.2)]


def test_frame_times_are_segment_midpoints_capped():
    segs = [Segment(i, float(i), float(i + 1)) for i in range(30)]
    picks = pick_frame_times(segs, max_frames=24)
    assert len(picks) == 24
    indices = [i for i, _ in picks]
    assert indices == sorted(set(indices))
    assert indices[0] == 0 and indices[-1] == 29
    assert dict(picks)[0] == 0.5


def test_frame_args_seek_before_input_and_downscale():
    args = build_frame_args("/in.mp4", 4.25, "/out.jpg")
    assert args[:4] == ["-ss", "4.250", "-i", "/in.mp4"]
    assert args[args.index("-vf") + 1] == "scale=512:-2"
    assert args[-1] == "/out.jpg"
