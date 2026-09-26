import uuid
from dataclasses import dataclass

import pydantic

from app.decision import ClipValue
from app.jobs.errors import ErrorCode, JobError
from app.schemas.analysis import ProposedClip
from app.schemas.edl import EDL, TrimOp

END_TOLERANCE_S = 0.05


@dataclass(frozen=True)
class Kept:
    clip: ProposedClip
    value: ClipValue
    relevance: float


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _covered(intervals: list[tuple[float, float]]) -> float:
    return sum(end - start for start, end in _merge(intervals))


def build_edl(
    media_id: uuid.UUID, media_duration: float, kept: list[Kept], target: float, min_len: float = 1.0
) -> EDL:
    """Greedy, deterministic selection: HIGH before MEDIUM, then by relevance, until the target duration is
    covered. Overlaps are merged so nothing plays twice; the last clip is trimmed to land on the target."""
    ranked = sorted(
        (k for k in kept if k.value is not ClipValue.LOW_VALUE),
        key=lambda k: (k.value is not ClipValue.HIGH_VALUE, -k.relevance, k.clip.start),
    )
    chosen: list[tuple[float, float]] = []
    for k in ranked:
        covered = _covered(chosen)
        remaining = target - covered
        if remaining < min_len:
            break
        start, end = k.clip.start, min(k.clip.end, media_duration)
        added = _covered([*chosen, (start, end)]) - covered
        if added <= 0:
            continue
        if added > remaining:
            end -= added - remaining
            if end - start < min_len:
                continue
        chosen.append((start, end))

    if not chosen:
        raise JobError(ErrorCode.NO_CLIPS_SELECTED, "no candidate clip was accepted for this edit")
    return EDL(
        source_media_id=media_id,
        operations=[TrimOp(start=round(s, 3), end=round(e, 3)) for s, e in _merge(chosen)],
    )


def validate_edl(edl: EDL, media_duration: float) -> None:
    for op in edl.operations:
        if op.end > media_duration + END_TOLERANCE_S:
            raise JobError(
                ErrorCode.INVALID_RANGE,
                f"operation {op.start}-{op.end} exceeds media duration {media_duration}",
            )


def load_edl(raw: str) -> EDL:
    try:
        return EDL.model_validate_json(raw)
    except pydantic.ValidationError as e:
        raise JobError(ErrorCode.INVALID_EDL, f"invalid EDL: {e}"[:2000]) from e
