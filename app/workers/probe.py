from dataclasses import asdict
from typing import Any

from sqlalchemy import update

from app.jobs.errors import ErrorCode, JobError
from app.jobs.states import Stage
from app.media.ffprobe import ProbeError, probe
from app.models import Media
from app.workers.context import JobContext, handler


@handler("probe")
async def probe_media(ctx: JobContext) -> dict[str, Any]:
    media = ctx.media
    if media.probe_status == "done":
        return {"skipped": True}

    await ctx.progress(Stage.PROBE, 0.0, "Probing media", force=True)
    src = ctx.storage.path(media.storage_key)
    if not src.is_file():
        raise JobError(ErrorCode.MISSING_SOURCE, f"source file missing: {media.storage_key}")
    try:
        meta = await probe(src)
    except ProbeError as e:
        raise JobError(ErrorCode(e.code), e.message) from e
    except TimeoutError as e:
        raise JobError(ErrorCode.TIMEOUT, "ffprobe timed out") from e

    async with ctx.session_factory() as s:
        await s.execute(
            update(Media)
            .where(Media.id == media.id)
            .values(
                duration_seconds=meta.duration_seconds,
                width=meta.width,
                height=meta.height,
                fps=meta.fps,
                rotation=meta.rotation,
                video_codec=meta.video_codec,
                audio_codec=meta.audio_codec,
                container=meta.container,
                bitrate=meta.bitrate,
                probe_status="done",
            )
        )
        await s.commit()
    return asdict(meta)
