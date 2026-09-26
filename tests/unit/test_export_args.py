from pathlib import Path

from app.media.export import build_concat_args, build_segment_args, concat_list
from app.media.profiles import PROFILES, scale_filter
from app.schemas.edl import TrimOp

SRC, DST = Path("/data/src.mp4"), Path("/data/.tmp-x-seg_000.mp4")


def test_segment_args_seek_trim_and_encode_with_profile():
    profile = PROFILES["EXPORT_DEFAULT"]
    args = build_segment_args(SRC, TrimOp(start=12.4, end=24.8), DST, profile, has_audio=True)
    assert args[:6] == ["-ss", "12.400", "-i", str(SRC), "-t", "12.400"]
    assert args[args.index("-vf") + 1] == scale_filter(1080)
    assert args[args.index("-preset") + 1] == "medium"
    # Uniform audio layout across segments is what makes a stream-copy concat valid.
    assert args[args.index("-ar") + 1] == "48000"
    assert args[args.index("-ac") + 1] == "2"
    assert args[-1] == str(DST)


def test_segment_args_without_audio():
    args = build_segment_args(SRC, TrimOp(start=0, end=5), DST, PROFILES["EXPORT_LOWER_RES"], has_audio=False)
    assert "-an" in args
    assert "-c:a" not in args
    assert args[args.index("-vf") + 1] == scale_filter(720)


def test_concat_list_and_args():
    listing = concat_list([Path("/data/a/seg_000.mp4"), Path("/data/a/seg_001.mp4")])
    assert listing == "file '/data/a/seg_000.mp4'\nfile '/data/a/seg_001.mp4'\n"
    args = build_concat_args(Path("/data/list.txt"), DST)
    assert args == ["-f", "concat", "-safe", "0", "-i", "/data/list.txt", "-c", "copy",
                    "-movflags", "+faststart", str(DST)]  # fmt: skip
