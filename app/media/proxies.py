from pathlib import Path

from app.media.profiles import EncodingProfile, scale_filter


def build_proxy_args(src: Path, dst: Path, profile: EncodingProfile, has_audio: bool) -> list[str]:
    audio = list(profile.audio_args) if has_audio else ["-an"]
    return [
        "-i", str(src),
        "-vf", scale_filter(profile.max_height),
        *profile.video_args,
        *audio,
        "-movflags", "+faststart",
        str(dst),
    ]  # fmt: skip
