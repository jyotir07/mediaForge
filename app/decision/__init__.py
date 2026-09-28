"""Bounded decision layer. The rest of the application depends only on these classes, never on Jev
directly, so Jev can be replaced without touching the media pipeline."""

import logging
from dataclasses import dataclass

from app.config import Settings
from app.decision.clip_classifier import ClipCandidate, ClipClassifier, ClipDecision, ClipValue
from app.decision.edit_validator import EditValidation, EditValidator, EditVerdict
from app.decision.jev import JevClient, JevLike
from app.decision.recovery_classifier import FailureObservation, RecoveryClassifier, RecoveryStrategy
from app.logging import log_event


@dataclass(frozen=True)
class DecisionLayer:
    jev: JevLike | None
    clip: ClipClassifier
    edit: EditValidator
    recovery: RecoveryClassifier

    @classmethod
    def with_jev(cls, jev: JevLike | None) -> "DecisionLayer":
        return cls(jev, ClipClassifier(jev), EditValidator(jev), RecoveryClassifier(jev))


def create_decision_layer(settings: Settings) -> DecisionLayer:
    jev: JevLike | None = None
    if settings.decision_backend == "jev":
        if settings.jevmodel_api_key is None:
            log_event("decision.jev_unconfigured", level=logging.WARNING, detail="JEVMODEL_API_KEY not set")
        else:
            jev = JevClient(settings.jevmodel_api_key.get_secret_value(), settings.jev_base_url)
    return DecisionLayer.with_jev(jev)


__all__ = [
    "ClipCandidate",
    "ClipClassifier",
    "ClipDecision",
    "ClipValue",
    "DecisionLayer",
    "EditValidation",
    "EditValidator",
    "EditVerdict",
    "FailureObservation",
    "RecoveryClassifier",
    "RecoveryStrategy",
    "create_decision_layer",
]
