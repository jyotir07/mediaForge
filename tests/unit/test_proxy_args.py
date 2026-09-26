from pathlib import Path

from app.media.profiles import PROFILES, scale_filter
from app.media.proxies import build_proxy_args
from app.media.thumbnails import build_thumbnail_args

SRC, DST = Path("/data/src.mp4"), Path("/data/.tmp-x-proxy.mp4")


def test_proxy_args_with_audio_use_profile_audio_and_scaling():
    profile = PROFILES["PROXY_STANDARD"]
    args = build_proxy_args(SRC, DST, profile, has_audio=True)
    assert args[:2] == ["-i", str(SRC)]
    assert args[args.index("-vf") + 1] == scale_filter(profile.max_height)
    assert "-an" not in args
    assert args[args.index("-c:a") + 1] == "aac"
    assert args[-1] == str(DST)
    assert "+faststart" in args


def test_proxy_args_without_audio_drop_audio():
    args = build_proxy_args(SRC, DST, PROFILES["PROXY_STANDARD"], has_audio=False)
    assert "-an" in args
    assert "-c:a" not in args


def test_thumbnail_contact_sheet_is_single_tiled_frame():
    args = build_thumbnail_args(SRC, DST, duration=90.0, count=9)
    vf = args[args.index("-vf") + 1]
    assert vf == "fps=9/90.000,scale=320:-2,tile=3x3"
    assert args[args.index("-frames:v") + 1] == "1"
    assert args[-1] == str(DST)
