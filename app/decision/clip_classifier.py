import logging
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from langchain_typesafe import Score

from app.decision.jev import DEFAULT_TIMEOUT_S, JevLike, ask_safely
from app.logging import log_event


class ClipValue(StrEnum):
    LOW_VALUE = "LOW_VALUE"
    MEDIUM_VALUE = "MEDIUM_VALUE"
    HIGH_VALUE = "HIGH_VALUE"


_LEVELS = [ClipValue.LOW_VALUE, ClipValue.MEDIUM_VALUE, ClipValue.HIGH_VALUE]

VALUE_QUESTION = Score(
    instructions="How valuable is this candidate clip for the requested edit?",
    criteria=[
        "Low value: off-topic, blank, transitional, or weak content for this request.",
        "Medium value: relevant, but not among the strongest moments.",
        "High value: a clear, strong moment that directly serves the request.",
    ],
)


@dataclass(frozen=True)
class ClipCandidate:
    start: float
    end: float
    reason: str
    signals: dict[str, float]
    goal: str

    def state(self) -> dict[str, Any]:
        return {
            "clip": {"start": self.start, "end": self.end, "duration": round(self.end - self.start, 3)},
            "reason": self.reason,
            "signals": self.signals,
            "request": {"goal": self.goal},
        }


@dataclass(frozen=True)
class ClipDecision:
    value: ClipValue
    confidence: float | None
    source: Literal["jev", "fallback"]


class ClipClassifier:
    def __init__(
        self, jev: JevLike | None, timeout_s: float = DEFAULT_TIMEOUT_S, min_confidence: float = 0.5
    ):
        self._jev = jev
        self._timeout_s = timeout_s
        self._min_confidence = min_confidence

    async def classify(self, candidate: ClipCandidate) -> ClipDecision:
        response = await ask_safely(
            self._jev, "clip", candidate.state(), {"value": VALUE_QUESTION}, self._timeout_s
        )
        answer = response.scores.get("value") if response else None
        if answer is None or not math.isfinite(answer.score) or not -0.5 <= answer.score <= 2.5:
            if response is not None:
                log_event(
                    "decision.unexpected", level=logging.WARNING, point="clip", answer=repr(answer)[:200]
                )
            return self._fallback(candidate)

        level = round(answer.score)
        # A low-confidence verdict should not crowd out a confident one, so it is treated one level lower.
        if answer.confidence < self._min_confidence and level > 0:
            level -= 1
        return ClipDecision(_LEVELS[level], answer.confidence, "jev")

    @staticmethod
    def _fallback(candidate: ClipCandidate) -> ClipDecision:
        s = candidate.signals
        score = 0.6 * s.get("llm_relevance", 0.0) + 0.4 * s.get("audio_presence", 0.0)
        value = (
            ClipValue.HIGH_VALUE
            if score >= 0.7
            else ClipValue.MEDIUM_VALUE
            if score >= 0.4
            else ClipValue.LOW_VALUE
        )
        return ClipDecision(value, None, "fallback")
