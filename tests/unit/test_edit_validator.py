import pytest
from langchain_typesafe import ChoiceAnswer, ClassifierResponse

from app.decision.edit_validator import EditValidator, EditVerdict, clamp_to_duration, hard_validate
from app.schemas.analysis import ProposedClip
from tests.unit.test_clip_classifier import FakeJev, score_response

DURATION = 60.0


def op(start: float, end: float) -> ProposedClip:
    return ProposedClip(start=start, end=end, reason="r", source_segments=[0])


def choice_response(label: str, confidence: float = 0.9) -> ClassifierResponse:
    return ClassifierResponse(
        model="jev-latest",
        answers={
            "verdict": ChoiceAnswer(
                type="choice", choice=label, probabilities={label: 0.9}, confidence=confidence
            )
        },
    )


@pytest.mark.parametrize(
    ("clip", "expected"),
    [
        (op(-1, 5), EditVerdict.INVALID_RANGE),
        (op(10, 5), EditVerdict.INVALID_RANGE),
        (op(10, 10), EditVerdict.INVALID_RANGE),
        (op(10, 90), EditVerdict.INVALID_RANGE),  # past the end of the media
        (op(10, 10.3), EditVerdict.INVALID_DURATION),
        (op(0, 59.5), None),
        (op(10, 20), None),
    ],
)
def test_hard_validation(clip, expected):
    assert hard_validate(clip, DURATION) == expected


def test_too_long_clip_is_invalid_duration():
    assert hard_validate(op(0, 70), 100.0, max_len=60.0) is EditVerdict.INVALID_DURATION


def test_tiny_overshoot_past_end_is_clamped():
    clamped = clamp_to_duration(op(50, 60.04), DURATION)
    assert clamped.end == DURATION
    assert clamp_to_duration(op(50, 61), DURATION).end == 61  # real overshoot is left for validation


async def test_hard_failure_never_reaches_jev():
    jev = FakeJev(choice_response("VALID"))
    result = await EditValidator(jev).validate(op(-1, 5), goal="g", accepted=[], media_duration=DURATION)
    assert result.verdict is EditVerdict.INVALID_RANGE
    assert result.source == "rules"
    assert jev.calls == []


async def test_jev_can_reject_a_hard_valid_clip():
    jev = FakeJev(choice_response("LOW_VALUE"))
    result = await EditValidator(jev).validate(op(10, 20), goal="g", accepted=[], media_duration=DURATION)
    assert (result.verdict, result.source) == (EditVerdict.LOW_VALUE, "jev")
    [(state, questions)] = jev.calls
    assert state["operation"] == {"type": "trim", "start": 10.0, "end": 20.0}
    assert state["media_duration"] == DURATION


@pytest.mark.parametrize(
    "jev",
    [
        FakeJev(choice_response("rm -rf /")),  # label outside the allowed set
        FakeJev(choice_response("VALID", confidence=0.2)),  # too unsure to act on
        FakeJev(score_response(1.0, key="verdict")),  # wrong answer type
        FakeJev(exc=TimeoutError()),
        None,
    ],
)
async def test_jev_failures_fall_back_to_rules(jev):
    result = await EditValidator(jev).validate(op(10, 20), goal="g", accepted=[], media_duration=DURATION)
    assert (result.verdict, result.source) == (EditVerdict.VALID, "fallback")


async def test_fallback_flags_heavy_overlap_as_duplicate():
    v = EditValidator(None)
    dup = await v.validate(op(12, 22), goal="g", accepted=[op(10, 20)], media_duration=DURATION)
    fresh = await v.validate(op(19, 29), goal="g", accepted=[op(10, 20)], media_duration=DURATION)
    assert dup.verdict is EditVerdict.DUPLICATE_CONTENT
    assert fresh.verdict is EditVerdict.VALID
