import asyncio
import pathlib

import pytest
from langchain_typesafe import ChoiceAnswer, ClassifierResponse, ScoreAnswer

from app.decision.clip_classifier import ClipCandidate, ClipClassifier, ClipValue


class FakeJev:
    """Stands in for JevClient: returns a scripted ClassifierResponse or raises."""

    def __init__(self, result=None, exc=None, delay=0.0):
        self.result, self.exc, self.delay = result, exc, delay
        self.calls = []

    async def ask(self, state, questions):
        self.calls.append((state, questions))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.result


def score_response(score: float, confidence: float = 0.9, key: str = "value") -> ClassifierResponse:
    return ClassifierResponse(
        model="jev-latest",
        answers={
            key: ScoreAnswer(
                type="score",
                score=score,
                legend={0: "low", 1: "medium", 2: "high"},
                probabilities={0: 0.1, 1: 0.2, 2: 0.7},
                confidence=confidence,
            )
        },
    )


CANDIDATE = ClipCandidate(
    start=42.3,
    end=51.8,
    reason="Clearest explanation of the architecture.",
    signals={"llm_relevance": 0.9, "audio_presence": 0.95},
    goal="technical highlight reel",
)


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.0, ClipValue.LOW_VALUE),
        (0.9, ClipValue.MEDIUM_VALUE),
        (1.6, ClipValue.HIGH_VALUE),
        (2.0, ClipValue.HIGH_VALUE),
    ],
)
async def test_jev_score_maps_to_clip_value(score, expected):
    jev = FakeJev(score_response(score))
    decision = await ClipClassifier(jev).classify(CANDIDATE)
    assert decision.value is expected
    assert decision.source == "jev"
    assert decision.confidence == 0.9


async def test_state_sent_to_jev_is_structured_candidate():
    jev = FakeJev(score_response(2.0))
    await ClipClassifier(jev).classify(CANDIDATE)
    [(state, questions)] = jev.calls
    assert state["clip"] == {"start": 42.3, "end": 51.8, "duration": 9.5}
    assert state["signals"] == {"llm_relevance": 0.9, "audio_presence": 0.95}
    assert state["request"] == {"goal": "technical highlight reel"}
    assert list(questions) == ["value"]


async def test_low_confidence_high_is_downgraded():
    decision = await ClipClassifier(FakeJev(score_response(2.0, confidence=0.3))).classify(CANDIDATE)
    assert decision.value is ClipValue.MEDIUM_VALUE
    assert decision.source == "jev"


FALLBACK_HIGH = ClipValue.HIGH_VALUE  # 0.6*0.9 + 0.4*0.95 = 0.92


@pytest.mark.parametrize(
    "jev",
    [
        FakeJev(exc=RuntimeError("network down")),
        FakeJev(score_response(2.0, key="unexpected")),  # answer missing
        FakeJev(score_response(7.0)),  # out of the rubric's range
        FakeJev(score_response(float("nan"))),
        FakeJev(
            ClassifierResponse(
                model="jev-latest",
                answers={
                    "value": ChoiceAnswer(
                        type="choice", choice="MAYBE", probabilities={"MAYBE": 1.0}, confidence=1.0
                    )
                },
            )
        ),  # fmt: skip
        FakeJev(score_response(2.0), delay=5),  # too slow
        None,  # Jev not configured
    ],
)
async def test_any_jev_failure_falls_back_deterministically(jev):
    decision = await ClipClassifier(jev, timeout_s=0.2).classify(CANDIDATE)
    assert decision.source == "fallback"
    assert decision.value is FALLBACK_HIGH
    assert decision.confidence is None


@pytest.mark.parametrize(
    ("relevance", "audio", "expected"),
    [(0.9, 0.95, ClipValue.HIGH_VALUE), (0.5, 0.5, ClipValue.MEDIUM_VALUE), (0.1, 0.2, ClipValue.LOW_VALUE)],
)
async def test_fallback_thresholds(relevance, audio, expected):
    c = ClipCandidate(0, 5, "r", {"llm_relevance": relevance, "audio_presence": audio}, "goal")
    assert (await ClipClassifier(None).classify(c)).value is expected


def test_only_the_decision_package_imports_jev():
    app_dir = pathlib.Path(__file__).parents[2] / "app"
    offenders = [
        str(p.relative_to(app_dir))
        for p in app_dir.rglob("*.py")
        if "langchain_typesafe" in p.read_text() and p.parent.name != "decision"
    ]
    assert offenders == []
