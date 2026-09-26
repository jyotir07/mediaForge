"""Retry policy: maps a failure to a RetryDecision. Jev only chooses among predefined strategies, and each
strategy maps to a predefined encoding profile name; nothing it returns becomes an ffmpeg argument."""

from typing import Any, Protocol

from app.decision.recovery_classifier import FailureObservation, RecoveryStrategy
from app.jobs.errors import JobError, Recoverability, recoverability
from app.jobs.service import RetryDecision
from app.logging import log_event

STRATEGY_TO_PROFILE: dict[RecoveryStrategy, str | None] = {
    RecoveryStrategy.RETRY_SAME: None,
    RecoveryStrategy.RETRY_FALLBACK_CODEC: "EXPORT_FALLBACK_CODEC",
    RecoveryStrategy.RETRY_LOWER_RESOLUTION: "EXPORT_LOWER_RES",
}

# Only exports have alternative profiles to fall back to.
FALLBACK_JOB_TYPES = frozenset({"export"})


class RecoveryDecider(Protocol):
    async def classify(self, observation: FailureObservation) -> tuple[RecoveryStrategy, str]: ...


async def decide_retry(err: JobError, job: Any, media: Any, recovery: RecoveryDecider) -> RetryDecision:
    kind = recoverability(err.code)
    if kind is Recoverability.FATAL:
        return RetryDecision(retry=False)
    if kind is Recoverability.RETRY or job.type not in FALLBACK_JOB_TYPES:
        return RetryDecision(retry=True, strategy=RecoveryStrategy.RETRY_SAME)
    if job.attempt >= job.max_attempts:
        return RetryDecision(retry=False, strategy=RecoveryStrategy.DEAD_LETTER)

    observation = FailureObservation(
        error=err.code,
        codec=media.video_codec or "unknown",
        resolution=f"{media.width}x{media.height}",
        duration=media.duration_seconds or 0.0,
        attempt=job.attempt,
        job_type=job.type,
        profile=job.config.get("profile_override", "EXPORT_DEFAULT"),
    )
    strategy, source = await recovery.classify(observation)
    log_event("decision.recovery", strategy=strategy, source=source, error_code=err.code, attempt=job.attempt)
    if strategy is RecoveryStrategy.DEAD_LETTER:
        return RetryDecision(retry=False, strategy=strategy)
    return RetryDecision(retry=True, profile_override=STRATEGY_TO_PROFILE[strategy], strategy=strategy)
