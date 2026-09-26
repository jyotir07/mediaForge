import logging
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Literal

from langchain_typesafe import Choice

from app.decision.jev import DEFAULT_TIMEOUT_S, JevLike, ask_safely
from app.logging import log_event


class RecoveryStrategy(StrEnum):
    RETRY_SAME = "RETRY_SAME"
    RETRY_FALLBACK_CODEC = "RETRY_FALLBACK_CODEC"
    RETRY_LOWER_RESOLUTION = "RETRY_LOWER_RESOLUTION"
    DEAD_LETTER = "DEAD_LETTER"


STRATEGY_QUESTION = Choice(
    instructions="A media export failed. Which predefined recovery strategy should be applied?",
    criteria={
        "RETRY_SAME": "Transient failure; retrying with the same settings is likely to work.",
        "RETRY_FALLBACK_CODEC": "The encoder or codec failed; retry with a simpler, compatible encoding.",
        "RETRY_LOWER_RESOLUTION": "The export is too resource-heavy; retry at a lower resolution.",
        "DEAD_LETTER": "Retrying is unlikely to help; stop and report the failure.",
    },
)

_LADDER = {1: RecoveryStrategy.RETRY_FALLBACK_CODEC, 2: RecoveryStrategy.RETRY_LOWER_RESOLUTION}


@dataclass(frozen=True)
class FailureObservation:
    error: str
    codec: str
    resolution: str
    duration: float
    attempt: int
    job_type: str
    profile: str


class RecoveryClassifier:
    def __init__(
        self, jev: JevLike | None, timeout_s: float = DEFAULT_TIMEOUT_S, min_confidence: float = 0.5
    ):
        self._jev = jev
        self._timeout_s = timeout_s
        self._min_confidence = min_confidence

    async def classify(
        self, observation: FailureObservation
    ) -> tuple[RecoveryStrategy, Literal["jev", "fallback"]]:
        response = await ask_safely(
            self._jev, "recovery", asdict(observation), {"strategy": STRATEGY_QUESTION}, self._timeout_s
        )
        answer = response.choices.get("strategy") if response else None
        if answer is not None and answer.choice in RecoveryStrategy.__members__:
            if answer.confidence >= self._min_confidence:
                return RecoveryStrategy(answer.choice), "jev"
        elif response is not None:
            log_event(
                "decision.unexpected", level=logging.WARNING, point="recovery", answer=repr(answer)[:200]
            )
        # Deterministic ladder: simpler encoding first, then smaller output, then give up.
        return _LADDER.get(observation.attempt, RecoveryStrategy.DEAD_LETTER), "fallback"
