"""The only place encoder arguments are defined. Decisions (Jev, retries) pick a profile by name;
they never contribute arguments."""

from dataclasses import dataclass


@dataclass(frozen=True)
class EncodingProfile:
    name: str
    max_height: int
    video_args: tuple[str, ...]
    audio_args: tuple[str, ...]


_AAC = ("-c:a", "aac", "-b:a", "160k")

PROFILES: dict[str, EncodingProfile] = {
    p.name: p
    for p in (
        EncodingProfile(
            "PROXY_STANDARD",
            1080,
            # Short GOP so editors can seek the proxy quickly.
            ("-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", "30", "-pix_fmt", "yuv420p"),
            ("-c:a", "aac", "-b:a", "128k"),
        ),
        EncodingProfile(
            "EXPORT_DEFAULT",
            1080,
            ("-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"),
            _AAC,
        ),
        EncodingProfile(
            "EXPORT_FALLBACK_CODEC",
            1080,
            (
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-profile:v",
                "baseline",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
            ),
            _AAC,
        ),
        EncodingProfile(
            "EXPORT_LOWER_RES",
            720,
            ("-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"),
            _AAC,
        ),
    )
}


def scale_filter(max_height: int) -> str:
    # -2 keeps the aspect ratio with an even width (required by yuv420p); min() prevents upscaling.
    return f"scale=-2:'min({max_height},ih)'"
