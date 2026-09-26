import asyncio
import json
from pathlib import Path
from typing import Any

from app.ai.llm import ImagePart, Part, TextPart, structured
from app.ai.prompts import SCENE_ANALYSIS_SYSTEM
from app.jobs.errors import ErrorCode, JobError
from app.jobs.states import Stage
from app.media.audio import audio_presence, detect_silences
from app.media.frames import build_frame_args, pick_frame_times
from app.media.scenes import Segment, build_segments, detect_scene_cuts
from app.schemas.analysis import SceneAnalysis
from app.storage.local import media_key
from app.workers.context import JobContext, handler
from app.workers.steps import ffmpeg_timeout, logged, produce, upsert_artifact

ANALYSIS_VERSION = 1


def analysis_key(media_id: Any) -> str:
    return media_key(media_id, "analysis", f"v{ANALYSIS_VERSION}.json")


def _analysis_source(ctx: JobContext) -> Path:
    # Analyze the proxy when it exists: same content, far cheaper to decode than a 4K source.
    proxy = media_key(ctx.media.id, "proxy", "PROXY_STANDARD.mp4")
    if ctx.storage.exists(proxy):
        return ctx.storage.path(proxy)
    src = ctx.storage.path(ctx.media.storage_key)
    if not src.is_file():
        raise JobError(ErrorCode.MISSING_SOURCE, f"source file missing: {ctx.media.storage_key}")
    return src


async def _extract_frames(
    ctx: JobContext, src: Path, segments: list[Segment]
) -> dict[int, tuple[str, float]]:
    picks = pick_frame_times(segments)
    frames: dict[int, tuple[str, float]] = {}
    for n, (idx, t) in enumerate(picks):
        key = media_key(ctx.media.id, "frames", f"{idx:03}.jpg")
        lo = 0.3 + 0.7 * n / len(picks)
        await produce(
            ctx,
            key,
            lambda tmp, t=t: build_frame_args(src, t, tmp),  # type: ignore[misc]
            step=f"frame-{idx}",
            stage=Stage.FRAME_EXTRACTION,
            message="Extracting representative frames",
            total_seconds=None,
            progress_range=(lo, lo),
        )
        await upsert_artifact(ctx, "FRAME", key, "image/jpeg", {"segment_index": idx, "t": t})
        frames[idx] = (key, t)
    return frames


async def _llm_parts(
    ctx: JobContext,
    duration: float,
    segments: list[Segment],
    presence: dict[int, float],
    frames: dict[int, Any],
) -> list[Part]:
    parts: list[Part] = [TextPart(f"Video duration: {duration:.2f}s. {len(segments)} segments follow.")]
    for s in segments:
        line = f"Segment {s.index}: {s.start:.2f}s-{s.end:.2f}s, audio presence {presence[s.index]:.2f}"
        if s.index in frames:
            key, t = frames[s.index]
            parts.append(TextPart(f"{line}, frame at {t:.2f}s:"))
            parts.append(ImagePart(await asyncio.to_thread(ctx.storage.path(key).read_bytes)))
        else:
            parts.append(TextPart(f"{line} (no frame)"))
    return parts


@handler("analyze")
async def analyze_media(ctx: JobContext) -> dict[str, Any]:
    media = ctx.media
    key = analysis_key(media.id)
    if ctx.storage.exists(key):
        await upsert_artifact(ctx, "ANALYSIS", key, "application/json")
        return {"analysis_key": key, "reused": True}
    if ctx.llm is None:
        raise JobError(ErrorCode.LLM_REQUEST_FAILED, "no LLM backend configured")

    src = _analysis_source(ctx)
    duration = media.duration_seconds or 0.0
    timeout_s = ffmpeg_timeout(duration)

    await ctx.progress(Stage.FRAME_EXTRACTION, 0.0, "Detecting scenes", force=True)
    async with logged(ctx, "scenes") as log_path:
        cuts = await detect_scene_cuts(src, log_path=log_path, timeout_s=timeout_s)
    segments = build_segments(cuts, duration)

    silences: list[tuple[float, float]] = []
    if media.audio_codec:
        await ctx.progress(Stage.FRAME_EXTRACTION, 0.2, "Measuring audio", force=True)
        async with logged(ctx, "silence") as log_path:
            silences = await detect_silences(src, duration, log_path=log_path, timeout_s=timeout_s)
    presence = {s.index: audio_presence(s, silences) if media.audio_codec else 0.0 for s in segments}

    frames = await _extract_frames(ctx, src, segments)

    await ctx.progress(Stage.ANALYSIS, 0.0, "Analyzing footage", force=True)
    parts = await _llm_parts(ctx, duration, segments, presence, frames)
    result = await structured(ctx.llm, system=SCENE_ANALYSIS_SYSTEM, parts=parts, schema=SceneAnalysis)
    described = {sc.segment_index: sc for sc in result.scenes}

    doc = {
        "version": ANALYSIS_VERSION,
        "media_id": str(media.id),
        "duration": duration,
        "overall_summary": result.overall_summary,
        "segments": [
            {
                "index": s.index,
                "start": s.start,
                "end": s.end,
                "frame_key": frames[s.index][0] if s.index in frames else None,
                "summary": described[s.index].summary if s.index in described else "",
                "signals": {
                    "audio_presence": round(presence[s.index], 3),
                    # A segment the model skipped gets no credit rather than a guessed score.
                    "llm_relevance": described[s.index].relevance if s.index in described else 0.0,
                },
            }
            for s in segments
        ],
    }
    tmp = ctx.storage.tmp_path(key)
    try:
        await asyncio.to_thread(tmp.write_text, json.dumps(doc, indent=2))
        ctx.storage.commit(tmp, key)
    finally:
        tmp.unlink(missing_ok=True)
    await upsert_artifact(ctx, "ANALYSIS", key, "application/json", {"segments": len(segments)})
    return {"analysis_key": key, "segments": len(segments)}
