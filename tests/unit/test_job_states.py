import random

import pytest

from app.jobs.errors import ErrorCode, JobError, Recoverability, recoverability
from app.jobs.states import ALLOWED, IllegalTransition, JobStatus, assert_transition, backoff_seconds


@pytest.mark.parametrize(("src", "dst"), [(s, d) for s, ds in ALLOWED.items() for d in ds])
def test_allowed_transitions_pass(src, dst):
    assert_transition(src, dst)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (JobStatus.SUCCEEDED, JobStatus.RUNNING),
        (JobStatus.FAILED, JobStatus.RUNNING),
        (JobStatus.QUEUED, JobStatus.SUCCEEDED),
        (JobStatus.RETRYING, JobStatus.SUCCEEDED),
    ],
)
def test_illegal_transitions_raise(src, dst):
    with pytest.raises(IllegalTransition):
        assert_transition(src, dst)


def test_terminal_states_have_no_exits():
    assert JobStatus.SUCCEEDED not in ALLOWED
    assert JobStatus.FAILED not in ALLOWED


def test_backoff_is_exponential_and_capped_without_jitter():
    delays = [backoff_seconds(a, base=5, cap=60, jitter=0) for a in range(1, 7)]
    assert delays == [5, 10, 20, 40, 60, 60]


def test_backoff_jitter_stays_within_20_percent():
    rng = random.Random(42)
    for _ in range(200):
        d = backoff_seconds(2, base=5, cap=300, rng=rng)
        assert 8.0 <= d <= 12.0


@pytest.mark.parametrize("code", list(ErrorCode))
def test_every_error_code_has_a_recoverability(code):
    assert isinstance(recoverability(code), Recoverability)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ErrorCode.WORKER_LOST, Recoverability.RETRY),
        (ErrorCode.TIMEOUT, Recoverability.RETRY),
        (ErrorCode.ENCODER_FAILURE, Recoverability.FALLBACK),
        (ErrorCode.CORRUPT_SOURCE, Recoverability.FATAL),
        (ErrorCode.MISSING_SOURCE, Recoverability.FATAL),
        (ErrorCode.LLM_OUTPUT_INVALID, Recoverability.FATAL),
    ],
)
def test_spec_examples_classified(code, expected):
    assert recoverability(code) is expected


def test_job_error_carries_code_message_and_observation():
    err = JobError(ErrorCode.ENCODER_FAILURE, "boom", observation={"codec": "hevc"})
    assert err.code is ErrorCode.ENCODER_FAILURE
    assert str(err) == "boom"
    assert err.observation == {"codec": "hevc"}
