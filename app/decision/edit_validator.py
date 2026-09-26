import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from langchain_typesafe import Choice

from app.decision.jev import DEFAULT_TIMEOUT_S, JevLike, ask_safely
from app.logging import log_event
from app.schemas.analysis import ProposedClip

END_TOLERANCE_S = 0.05
DUPLICATE_OVERLAP = 0.5


class EditVerdict(StrEnum):
    VALID = "VALID"
    INVALID_DURATION = "INVALID_DURATION"
    INVALID_RANGE = "INVALID_RANGE"
    LOW_VALUE = "LOW_VALUE"
    DUPLICATE_CONTENT = "DUPLICATE_CONTENT"


VERDICT_QUESTION = Choice(
    instructions="Should this proposed trim be used for the requested edit, given the accepted clips?",
    criteria={
        "VALID": "A sensible, usable trim for the request.",
        "INVALID_DURATION": "Too short or too long to work as a clip in this edit.",
        "INVALID_RANGE": "The time range does not make sense for this media.",
        "LOW_VALUE": "Usable, but the content does not serve the request.",
        "DUPLICATE_CONTENT": "Mostly repeats content from an already accepted clip.",
    },
)


@dataclass(frozen=True)
class EditValidation:
    verdict: EditVerdict
    source: Literal["rules", "jev", "fallback"]
    confidence: float | None = None


def clamp_to_duration(op: ProposedClip, media_duration: float) -> ProposedClip:
    """Absorb float noise at the tail (e.g. 60.04 on a 60.0s video); real overshoot is left to validation."""
    if media_duration < op.end <= media_duration + END_TOLERANCE_S:
        return op.model_copy(update={"end": media_duration})
    return op


def hard_validate(
    op: ProposedClip, media_duration: float, min_len: float = 1.0, max_len: float = 60.0
) -> EditVerdict | None:
    """Constraints no model may override: start >= 0, end > start, end <= duration, sane length."""
    if op.start < 0 or op.end <= op.start or op.end > media_duration:
        return EditVerdict.INVALID_RANGE
    if not min_len <= op.end - op.start <= max_len:
        return EditVerdict.INVALID_DURATION
    return None


def _overlap(a: ProposedClip, b: ProposedClip) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


class EditValidator:
    def __init__(
        self, jev: JevLike | None, timeout_s: float = DEFAULT_TIMEOUT_S, min_confidence: float = 0.5
    ):
        self._jev = jev
        self._timeout_s = timeout_s
        self._min_confidence = min_confidence

    async def validate(
        self, op: ProposedClip, *, goal: str, accepted: list[ProposedClip], media_duration: float
    ) -> EditValidation:
        op = clamp_to_duration(op, media_duration)
        if (hard := hard_validate(op, media_duration)) is not None:
            return EditValidation(hard, "rules")

        state = {
            "operation": {"type": "trim", "start": op.start, "end": op.end},
            "reason": op.reason,
            "request": {"goal": goal},
            "media_duration": media_duration,
            "accepted_clips": [{"start": a.start, "end": a.end} for a in accepted],
        }
        response = await ask_safely(self._jev, "edit", state, {"verdict": VERDICT_QUESTION}, self._timeout_s)
        answer = response.choices.get("verdict") if response else None
        if answer is not None and answer.choice in EditVerdict.__members__:
            if answer.confidence >= self._min_confidence:
                return EditValidation(EditVerdict(answer.choice), "jev", answer.confidence)
        elif response is not None:
            log_event("decision.unexpected", level=logging.WARNING, point="edit", answer=repr(answer)[:200])
        return self._fallback(op, accepted)

    @staticmethod
    def _fallback(op: ProposedClip, accepted: list[ProposedClip]) -> EditValidation:
        length = op.end - op.start
        if any(_overlap(op, a) >= DUPLICATE_OVERLAP * length for a in accepted):
            return EditValidation(EditVerdict.DUPLICATE_CONTENT, "fallback")
        return EditValidation(EditVerdict.VALID, "fallback")
