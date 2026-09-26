from types import SimpleNamespace

import pytest
from langchain_typesafe import ChoiceAnswer, ClassifierResponse

from app.decision.recovery_classifier import FailureObservation, RecoveryClassifier, RecoveryStrategy
from app.jobs.errors import ErrorCode, JobError
from app.workers.recovery import decide_retry
from tests.unit.test_clip_classifier import FakeJev


def choice(label: str, confidence: float = 0.9) -> ClassifierResponse:
    return ClassifierResponse(
        model="jev-latest",
        answers={
            "strategy": ChoiceAnswer(
                type="choice", choice=label, probabilities={label: 0.9}, confidence=confidence
            )
        },
    )


def obs(attempt: int) -> FailureObservation:
    return FailureObservation(
        error="ENCODER_FAILURE", codec="hevc", resolution="3840x2160", duration=1050.0, attempt=attempt,
        job_type="export", profile="EXPORT_DEFAULT",
    )  # fmt: skip


async def test_jev_strategy_is_used():
    jev = FakeJev(choice("RETRY_LOWER_RESOLUTION"))
    strategy, source = await RecoveryClassifier(jev).classify(obs(1))
    assert (strategy, source) == (RecoveryStrategy.RETRY_LOWER_RESOLUTION, "jev")
    [(state, _)] = jev.calls
    assert state == {
        "error": "ENCODER_FAILURE", "codec": "hevc", "resolution": "3840x2160", "duration": 1050.0,
        "attempt": 1, "job_type": "export", "profile": "EXPORT_DEFAULT",
    }  # fmt: skip


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [
        (1, RecoveryStrategy.RETRY_FALLBACK_CODEC),
        (2, RecoveryStrategy.RETRY_LOWER_RESOLUTION),
        (3, RecoveryStrategy.DEAD_LETTER),
    ],
)
@pytest.mark.parametrize("jev", [FakeJev(choice("rm -rf /")), FakeJev(exc=ConnectionError()), None])
async def test_fallback_ladder_when_jev_unusable(jev, attempt, expected):
    strategy, source = await RecoveryClassifier(jev).classify(obs(attempt))
    assert (strategy, source) == (expected, "fallback")


class SpyRecovery:
    def __init__(self, strategy=RecoveryStrategy.RETRY_FALLBACK_CODEC):
        self.strategy = strategy
        self.calls = []

    async def classify(self, observation):
        self.calls.append(observation)
        return self.strategy, "jev"


def job(job_type="export", attempt=1, max_attempts=3, config=None):
    return SimpleNamespace(type=job_type, attempt=attempt, max_attempts=max_attempts, config=config or {})


MEDIA = SimpleNamespace(video_codec="h264", width=1920, height=1080, duration_seconds=30.0)


@pytest.mark.parametrize("code", [ErrorCode.CORRUPT_SOURCE, ErrorCode.INVALID_EDL, ErrorCode.MISSING_SOURCE])
async def test_fatal_errors_never_consult_jev(code):
    spy = SpyRecovery()
    decision = await decide_retry(JobError(code, "x"), job(), MEDIA, spy)
    assert decision.retry is False
    assert spy.calls == []


async def test_plain_recoverable_errors_retry_same_without_jev():
    spy = SpyRecovery()
    decision = await decide_retry(JobError(ErrorCode.TIMEOUT, "x"), job(), MEDIA, spy)
    assert (decision.retry, decision.strategy, decision.profile_override) == (True, "RETRY_SAME", None)
    assert spy.calls == []


async def test_encoder_failure_maps_jev_strategy_to_predefined_profile():
    spy = SpyRecovery(RecoveryStrategy.RETRY_FALLBACK_CODEC)
    decision = await decide_retry(JobError(ErrorCode.ENCODER_FAILURE, "x"), job(), MEDIA, spy)
    assert decision.retry is True
    assert decision.profile_override == "EXPORT_FALLBACK_CODEC"
    assert decision.strategy == "RETRY_FALLBACK_CODEC"
    assert spy.calls[0].resolution == "1920x1080"


async def test_jev_dead_letter_stops_retrying():
    spy = SpyRecovery(RecoveryStrategy.DEAD_LETTER)
    decision = await decide_retry(JobError(ErrorCode.ENCODER_FAILURE, "x"), job(), MEDIA, spy)
    assert (decision.retry, decision.strategy) == (False, "DEAD_LETTER")


async def test_last_attempt_is_dead_letter_whatever_jev_would_say():
    spy = SpyRecovery(RecoveryStrategy.RETRY_SAME)
    decision = await decide_retry(
        JobError(ErrorCode.ENCODER_FAILURE, "x"), job(attempt=3, max_attempts=3), MEDIA, spy
    )
    assert (decision.retry, decision.strategy) == (False, "DEAD_LETTER")
    assert spy.calls == []


async def test_fallback_errors_outside_export_jobs_just_retry():
    spy = SpyRecovery()
    decision = await decide_retry(JobError(ErrorCode.ENCODER_FAILURE, "x"), job("proxy"), MEDIA, spy)
    assert (decision.retry, decision.profile_override) == (True, None)
    assert spy.calls == []
