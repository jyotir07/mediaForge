"""The only module that talks to Jev (TypeSafe's classifier model, via `langchain-typesafe`).

API verified against langchain-typesafe 0.0.1a3 source:
- TypeSafeClassifier(api_key=..., timeout=...) -> .ainvoke({"state": ..., "questions": {...}})
- Choice(instructions, criteria={label: description}) -> response.choices[id].choice / .confidence
- Score(instructions, criteria=[level0, level1, ...]) -> response.scores[id].score (fractional) / .confidence
- Errors derive from langchain_typesafe.client.TypeSafeError; a missing key raises ValueError at construction.
"""

import asyncio
import logging
from typing import Any, Protocol

from langchain_typesafe import ClassifierResponse, Question, TypeSafeClassifier

from app.logging import log_event

DEFAULT_TIMEOUT_S = 5.0


class JevLike(Protocol):
    async def ask(self, state: dict[str, Any], questions: dict[str, Question]) -> ClassifierResponse: ...


class JevClient:
    def __init__(self, api_key: str, timeout_s: float = DEFAULT_TIMEOUT_S):
        self._classifier = TypeSafeClassifier(api_key=api_key, timeout=timeout_s)

    async def ask(self, state: dict[str, Any], questions: dict[str, Question]) -> ClassifierResponse:
        return await self._classifier.ainvoke({"state": state, "questions": questions})


async def ask_safely(
    jev: JevLike | None,
    point: str,
    state: dict[str, Any],
    questions: dict[str, Question],
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ClassifierResponse | None:
    """Ask Jev, or return None on any failure. Callers then apply their deterministic fallback, so a Jev
    outage degrades decision quality but never blocks or crashes the pipeline."""
    if jev is None:
        return None
    try:
        return await asyncio.wait_for(jev.ask(state, questions), timeout_s)
    except Exception as exc:
        log_event("decision.jev_error", level=logging.WARNING, point=point, error=repr(exc)[:300])
        return None
