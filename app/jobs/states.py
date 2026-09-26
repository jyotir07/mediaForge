import random
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Stage(StrEnum):
    UPLOAD = "UPLOAD"
    PROBE = "PROBE"
    PROXY = "PROXY"
    FRAME_EXTRACTION = "FRAME_EXTRACTION"
    ANALYSIS = "ANALYSIS"
    EDIT_PLANNING = "EDIT_PLANNING"
    DECISION = "DECISION"
    EXPORT = "EXPORT"
    COMPLETED = "COMPLETED"


ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING}),
    JobStatus.RUNNING: frozenset({JobStatus.SUCCEEDED, JobStatus.RETRYING, JobStatus.FAILED}),
    JobStatus.RETRYING: frozenset({JobStatus.RUNNING, JobStatus.FAILED}),
}

TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED})


class IllegalTransition(Exception):
    pass


def assert_transition(src: JobStatus, dst: JobStatus) -> None:
    if dst not in ALLOWED.get(src, frozenset()):
        raise IllegalTransition(f"{src} -> {dst}")


def backoff_seconds(
    attempt: int,
    base: float = 5,
    cap: float = 300,
    jitter: float = 0.2,
    rng: random.Random | None = None,
) -> float:
    delay = min(cap, base * 2 ** (attempt - 1))
    if jitter:
        delay *= 1 + (rng or random).uniform(-jitter, jitter)  # noqa: S311 - jitter, not crypto
    return delay
