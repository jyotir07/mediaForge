from typing import Any

from app.jobs.errors import ErrorCode, JobError
from app.jobs.states import Stage
from app.media.profiles import PROFILES
from app.media.proxies import build_proxy_args
from app.media.thumbnails import build_thumbnail_args
from app.storage.local import media_key
from app.workers.context import JobContext, handler
from app.workers.steps import produce, upsert_artifact


@handler("proxy")
async def generate_proxy(ctx: JobContext) -> dict[str, Any]:
    media = ctx.media
    src = ctx.storage.path(media.storage_key)
    if not src.is_file():
        raise JobError(ErrorCode.MISSING_SOURCE, f"source file missing: {media.storage_key}")
    profile = PROFILES[ctx.job.config.get("profile", "PROXY_STANDARD")]
    duration = media.duration_seconds or 0.0

    proxy_key = media_key(media.id, "proxy", f"{profile.name}.mp4")
    await produce(
        ctx,
        proxy_key,
        lambda tmp: build_proxy_args(src, tmp, profile, has_audio=media.audio_codec is not None),
        step="proxy",
        stage=Stage.PROXY,
        message="Generating proxy",
        total_seconds=duration,
        progress_range=(0.0, 0.9),
    )
    await upsert_artifact(ctx, "PROXY", proxy_key, "video/mp4", {"profile": profile.name})

    # Sample thumbnails from the proxy: same frames, far cheaper to decode than a 4K source.
    proxy = ctx.storage.path(proxy_key)
    sheet_key = media_key(media.id, "thumbs", "contact.jpg")
    await produce(
        ctx,
        sheet_key,
        lambda tmp: build_thumbnail_args(proxy, tmp, duration),
        step="thumbnails",
        stage=Stage.PROXY,
        message="Generating thumbnails",
        total_seconds=duration,
        progress_range=(0.9, 1.0),
    )
    await upsert_artifact(ctx, "THUMBNAIL", sheet_key, "image/jpeg", {"kind": "contact_sheet"})
    return {"proxy_key": proxy_key, "contact_sheet_key": sheet_key}
