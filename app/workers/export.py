import asyncio
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from app.jobs.errors import ErrorCode, JobError
from app.jobs.states import Stage
from app.logging import log_event
from app.media.export import build_concat_args, build_segment_args, concat_list
from app.media.ffprobe import probe
from app.media.profiles import PROFILES
from app.models import Export
from app.schemas.edl import EDL
from app.services.edl import load_edl, validate_edl
from app.storage.local import media_key
from app.workers.context import JobContext, handler
from app.workers.edit import edl_key
from app.workers.steps import produce, upsert_artifact

ENCODE_SHARE = 0.95


async def _load_export_and_edl(ctx: JobContext) -> tuple[Export, EDL]:
    plan_id = uuid.UUID(ctx.job.config["edit_plan_id"])
    async with ctx.session_factory() as s:
        export = await s.scalar(select(Export).where(Export.edit_plan_id == plan_id))
    key = edl_key(ctx.media.id, plan_id)
    if export is None or not ctx.storage.exists(key):
        raise JobError(ErrorCode.INVALID_EDL, f"no export record or EDL for edit plan {plan_id}")
    edl = load_edl(await asyncio.to_thread(ctx.storage.path(key).read_text))
    if edl.source_media_id != ctx.media.id:
        raise JobError(ErrorCode.INVALID_EDL, "EDL belongs to a different media")
    validate_edl(edl, ctx.media.duration_seconds or 0.0)
    return export, edl


async def _write_text(ctx: JobContext, key: str, text: str) -> Path:
    tmp = ctx.storage.tmp_path(key)
    try:
        await asyncio.to_thread(tmp.write_text, text)
        ctx.storage.commit(tmp, key)
    finally:
        tmp.unlink(missing_ok=True)
    return ctx.storage.path(key)


@handler("export")
async def export_edit(ctx: JobContext) -> dict[str, Any]:
    media = ctx.media
    export, edl = await _load_export_and_edl(ctx)
    src = ctx.storage.path(media.storage_key)
    if not src.is_file():
        raise JobError(ErrorCode.MISSING_SOURCE, f"source file missing: {media.storage_key}")
    # The only way a decision (Jev recovery) influences encoding: selecting a predefined profile by name.
    profile = PROFILES[ctx.job.config.get("profile_override", "EXPORT_DEFAULT")]
    has_audio = media.audio_codec is not None

    base = media_key(media.id, "exports", str(export.id))
    final_key = f"{base}/final.mp4"
    if not ctx.storage.exists(final_key):
        # Segments are keyed by profile: a fallback retry never reuses output from the profile that failed,
        # while a same-profile retry resumes from the first missing segment.
        total, done = edl.duration, 0.0
        segments: list[Path] = []
        for i, op in enumerate(edl.operations):
            length = op.end - op.start
            seg_key = f"{base}/{profile.name}/seg_{i:03}.mp4"
            await produce(
                ctx,
                seg_key,
                lambda tmp, op=op: build_segment_args(src, op, tmp, profile, has_audio),  # type: ignore[misc]
                step=f"segment-{i}",
                stage=Stage.EXPORT,
                message=f"Encoding clip {i + 1} of {len(edl.operations)}",
                total_seconds=length,
                progress_range=(ENCODE_SHARE * done / total, ENCODE_SHARE * (done + length) / total),
            )
            segments.append(ctx.storage.path(seg_key))
            done += length

        list_path = await _write_text(ctx, f"{base}/{profile.name}/concat.txt", concat_list(segments))
        await produce(
            ctx,
            final_key,
            lambda tmp: build_concat_args(list_path, tmp),
            step="concat",
            stage=Stage.EXPORT,
            message="Joining clips",
            total_seconds=total,
            progress_range=(ENCODE_SHARE, 1.0),
        )

    meta = await probe(ctx.storage.path(final_key))
    artifact_id = await upsert_artifact(
        ctx, "EXPORT", final_key, "video/mp4", {"profile": profile.name, "clips": len(edl.operations)}
    )
    async with ctx.session_factory() as s:
        await s.execute(
            update(Export)
            .where(Export.id == export.id)
            .values(
                artifact_id=artifact_id,
                status="READY",
                duration_seconds=meta.duration_seconds,
                width=meta.width,
                height=meta.height,
                video_codec=meta.video_codec,
            )
        )
        await s.commit()
    log_event(
        "export.completed",
        export_id=export.id,
        profile=profile.name,
        duration_s=round(meta.duration_seconds, 2),
        size_bytes=ctx.storage.path(final_key).stat().st_size,
    )
    return {"export_id": str(export.id), "export_key": final_key, "profile": profile.name}
