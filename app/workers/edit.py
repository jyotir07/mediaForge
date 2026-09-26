import asyncio
import json
import uuid
from typing import Any

from sqlalchemy import select

from app.ai.llm import TextPart, structured
from app.ai.prompts import EDIT_PLANNING_SYSTEM
from app.decision import ClipCandidate, DecisionLayer, EditVerdict
from app.decision.edit_validator import clamp_to_duration
from app.jobs import service
from app.jobs.errors import ErrorCode, JobError
from app.jobs.states import Stage
from app.models import EditPlan, Export
from app.queue import redis_queue
from app.schemas.analysis import EditProposal, ProposedClip
from app.schemas.edl import EDL, TrimOp
from app.services.edl import Kept, build_edl, validate_edl
from app.storage.local import media_key
from app.workers.analysis import analysis_key
from app.workers.context import JobContext, handler
from app.workers.steps import upsert_artifact

MAX_CANDIDATES = 30


def edl_key(media_id: uuid.UUID, plan_id: uuid.UUID) -> str:
    return media_key(media_id, "edl", f"{plan_id}.json")


def segment_signals(analysis: dict[str, Any], start: float, end: float) -> dict[str, float]:
    """Overlap-weighted mean of the analysis signals across the segments a clip spans."""
    totals = {"llm_relevance": 0.0, "audio_presence": 0.0}
    covered = 0.0
    for seg in analysis["segments"]:
        overlap = max(0.0, min(end, seg["end"]) - max(start, seg["start"]))
        if overlap > 0:
            covered += overlap
            for k in totals:
                totals[k] += seg["signals"][k] * overlap
    return {k: round(v / covered, 3) if covered else 0.0 for k, v in totals.items()}


def _planning_prompt(analysis: dict[str, Any], request: str) -> str:
    segments = [
        {k: seg[k] for k in ("index", "start", "end", "summary")} | seg["signals"]
        for seg in analysis["segments"]
    ]
    return (
        f"Edit request: {request}\n\n"
        f"Video duration: {analysis['duration']:.2f}s\n"
        f"Overall summary: {analysis['overall_summary']}\n\n"
        f"Segments:\n{json.dumps(segments, indent=1)}"
    )


async def _load_analysis(ctx: JobContext) -> dict[str, Any]:
    key = analysis_key(ctx.media.id)
    if not ctx.storage.exists(key):
        raise JobError(ErrorCode.MISSING_SOURCE, "media has not been analyzed")
    return json.loads(await asyncio.to_thread(ctx.storage.path(key).read_text))


async def _decide(
    decisions: DecisionLayer, clips: list[ProposedClip], analysis: dict[str, Any], request: str
) -> tuple[list[dict[str, Any]], list[Kept]]:
    duration = analysis["duration"]
    records: list[dict[str, Any]] = []
    kept: list[Kept] = []
    accepted: list[ProposedClip] = []
    for clip in clips:
        validation = await decisions.edit.validate(
            clip, goal=request, accepted=accepted, media_duration=duration
        )
        record: dict[str, Any] = {
            "start": clip.start,
            "end": clip.end,
            "reason": clip.reason,
            "verdict": validation.verdict,
            "verdict_source": validation.source,
            "verdict_confidence": validation.confidence,
            "value": None,
            "value_source": None,
            "value_confidence": None,
        }
        if validation.verdict is EditVerdict.VALID:
            clip = clamp_to_duration(clip, duration)
            signals = segment_signals(analysis, clip.start, clip.end)
            decision = await decisions.clip.classify(
                ClipCandidate(clip.start, clip.end, clip.reason, signals, request)
            )
            record |= {
                "value": decision.value,
                "value_source": decision.source,
                "value_confidence": decision.confidence,
            }
            accepted.append(clip)
            kept.append(Kept(clip, decision.value, signals["llm_relevance"]))
        records.append(record)
    return records, kept


async def _plan(ctx: JobContext, request: str) -> EditPlan:
    if ctx.llm is None:
        raise JobError(ErrorCode.LLM_REQUEST_FAILED, "no LLM backend configured")
    if ctx.decisions is None:
        raise JobError(ErrorCode.DECISION_UNAVAILABLE, "no decision layer configured")
    analysis = await _load_analysis(ctx)
    duration = analysis["duration"]

    await ctx.progress(Stage.EDIT_PLANNING, 0.0, "Generating candidate edits", force=True)
    proposal = await structured(
        ctx.llm,
        system=EDIT_PLANNING_SYSTEM,
        parts=[TextPart(_planning_prompt(analysis, request))],
        schema=EditProposal,
    )

    await ctx.progress(Stage.DECISION, 0.0, f"Deciding on {len(proposal.clips)} candidate clips", force=True)
    records, kept = await _decide(ctx.decisions, proposal.clips[:MAX_CANDIDATES], analysis, request)
    edl = build_edl(ctx.media.id, duration, kept, target=min(proposal.target_duration_seconds, duration))
    validate_edl(edl, duration)

    plan = EditPlan(
        media_id=ctx.media.id,
        job_id=ctx.job.id,
        request=request,
        candidate_data=proposal.model_dump(),
        decisions=records,
        accepted_operations=[op.model_dump() for op in edl.operations],
    )
    async with ctx.session_factory() as s:
        s.add(plan)
        await s.commit()
    await ctx.progress(Stage.DECISION, 1.0, f"{len(edl.operations)} clips selected", force=True)
    return plan


async def _ensure_edl(ctx: JobContext, plan: EditPlan) -> str:
    key = edl_key(ctx.media.id, plan.id)
    if not ctx.storage.exists(key):
        edl = EDL(source_media_id=ctx.media.id, operations=[TrimOp(**op) for op in plan.accepted_operations])
        tmp = ctx.storage.tmp_path(key)
        try:
            await asyncio.to_thread(tmp.write_text, edl.model_dump_json(indent=2))
            ctx.storage.commit(tmp, key)
        finally:
            tmp.unlink(missing_ok=True)
    await upsert_artifact(ctx, "EDL", key, "application/json", {"edit_plan_id": str(plan.id)})
    return key


@handler("edit")
async def plan_edit(ctx: JobContext) -> dict[str, Any]:
    request: str = ctx.job.config["request"]
    async with ctx.session_factory() as s:
        # A retry after the plan was committed must not plan again (a second LLM call could disagree).
        plan = await s.scalar(select(EditPlan).where(EditPlan.job_id == ctx.job.id))
    if plan is None:
        plan = await _plan(ctx, request)
    key = await _ensure_edl(ctx, plan)

    async with ctx.session_factory() as s:
        export_job, created = await service.create_or_get(
            s, ctx.media.id, "export", {"edit_plan_id": str(plan.id)}
        )
        export = await s.scalar(select(Export).where(Export.edit_plan_id == plan.id))
        if export is None:
            export = Export(edit_plan_id=plan.id, media_id=ctx.media.id, job_id=export_job.id)
            s.add(export)
            await s.commit()
    if created:
        await redis_queue.enqueue(ctx.redis, "export", export_job.id)
    return {
        "edit_plan_id": str(plan.id),
        "edl_key": key,
        "export_id": str(export.id),
        "export_job_id": str(export_job.id),
        "clips_selected": len(plan.accepted_operations),
    }
