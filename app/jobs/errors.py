from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    # Recoverable: retry with backoff.
    WORKER_LOST = "WORKER_LOST"
    STORAGE_ERROR = "STORAGE_ERROR"
    PROCESS_INTERRUPTED = "PROCESS_INTERRUPTED"
    TIMEOUT = "TIMEOUT"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    # Recoverable with fallback: the recovery classifier picks a predefined profile.
    ENCODER_FAILURE = "ENCODER_FAILURE"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    # Non-recoverable.
    CORRUPT_SOURCE = "CORRUPT_SOURCE"
    UNSUPPORTED_CONTAINER = "UNSUPPORTED_CONTAINER"
    NO_VIDEO_STREAM = "NO_VIDEO_STREAM"
    INVALID_EDL = "INVALID_EDL"
    INVALID_RANGE = "INVALID_RANGE"
    MISSING_SOURCE = "MISSING_SOURCE"
    LLM_OUTPUT_INVALID = "LLM_OUTPUT_INVALID"
    NO_CLIPS_SELECTED = "NO_CLIPS_SELECTED"
    DECISION_UNAVAILABLE = "DECISION_UNAVAILABLE"
    LLM_REQUEST_FAILED = "LLM_REQUEST_FAILED"


class Recoverability(StrEnum):
    RETRY = "RETRY"
    FALLBACK = "FALLBACK"
    FATAL = "FATAL"


_RECOVERABILITY: dict[ErrorCode, Recoverability] = {
    ErrorCode.WORKER_LOST: Recoverability.RETRY,
    ErrorCode.STORAGE_ERROR: Recoverability.RETRY,
    ErrorCode.PROCESS_INTERRUPTED: Recoverability.RETRY,
    ErrorCode.TIMEOUT: Recoverability.RETRY,
    ErrorCode.LLM_UNAVAILABLE: Recoverability.RETRY,
    ErrorCode.ENCODER_FAILURE: Recoverability.FALLBACK,
    ErrorCode.RESOURCE_EXHAUSTED: Recoverability.FALLBACK,
    ErrorCode.CORRUPT_SOURCE: Recoverability.FATAL,
    ErrorCode.UNSUPPORTED_CONTAINER: Recoverability.FATAL,
    ErrorCode.NO_VIDEO_STREAM: Recoverability.FATAL,
    ErrorCode.INVALID_EDL: Recoverability.FATAL,
    ErrorCode.INVALID_RANGE: Recoverability.FATAL,
    ErrorCode.MISSING_SOURCE: Recoverability.FATAL,
    ErrorCode.LLM_OUTPUT_INVALID: Recoverability.FATAL,
    ErrorCode.NO_CLIPS_SELECTED: Recoverability.FATAL,
    ErrorCode.DECISION_UNAVAILABLE: Recoverability.FATAL,
    ErrorCode.LLM_REQUEST_FAILED: Recoverability.FATAL,
}


def recoverability(code: ErrorCode) -> Recoverability:
    return _RECOVERABILITY[code]


class JobError(Exception):
    def __init__(self, code: ErrorCode, message: str, observation: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.observation = observation or {}
