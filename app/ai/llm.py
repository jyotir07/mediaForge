"""Provider-agnostic structured LLM calls. The model only ever returns data validated against a
pydantic schema; nothing it produces is executed."""

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic
import pydantic
from anthropic.types.beta import BetaBase64ImageSourceParam, BetaImageBlockParam, BetaTextBlockParam
from pydantic import BaseModel

from app.config import Settings
from app.jobs.errors import ErrorCode, JobError

REPAIR_ATTEMPTS = 1
MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    jpeg: bytes

    def __repr__(self) -> str:
        return f"ImagePart({len(self.jpeg)} bytes)"


Part = TextPart | ImagePart


class LLMOutputInvalid(Exception):
    """The model answered, but not with data matching the schema. Worth one repair round."""


class LLMClient(Protocol):
    async def generate[T: BaseModel](self, *, system: str, parts: Sequence[Part], schema: type[T]) -> T: ...


async def structured[T: BaseModel](
    llm: LLMClient, *, system: str, parts: Sequence[Part], schema: type[T]
) -> T:
    attempt_parts = list(parts)
    for attempt in range(REPAIR_ATTEMPTS + 1):
        try:
            return await llm.generate(system=system, parts=attempt_parts, schema=schema)
        except LLMOutputInvalid as err:
            if attempt == REPAIR_ATTEMPTS:
                raise JobError(ErrorCode.LLM_OUTPUT_INVALID, f"model output invalid: {err}"[:2000]) from err
            attempt_parts = [
                *parts,
                TextPart(
                    "Your previous answer did not match the required schema. "
                    f"Validation error:\n{str(err)[:1500]}\nReturn a corrected answer."
                ),
            ]
    raise AssertionError("unreachable")


class AnthropicLLM:
    def __init__(self, settings: Settings, client: Any = None):
        self.model = settings.llm_model
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        # Without an explicit key the SDK falls back to its own credential chain (env, ant auth profile).
        self._client = client or anthropic.AsyncAnthropic(api_key=key, timeout=120.0, max_retries=2)

    def __repr__(self) -> str:
        return f"AnthropicLLM(model={self.model!r})"

    @staticmethod
    def _content(parts: Sequence[Part]) -> list[BetaTextBlockParam | BetaImageBlockParam]:
        blocks: list[BetaTextBlockParam | BetaImageBlockParam] = []
        for p in parts:
            if isinstance(p, TextPart):
                blocks.append({"type": "text", "text": p.text})
            else:
                data = base64.standard_b64encode(p.jpeg).decode()
                source: BetaBase64ImageSourceParam = {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": data,
                }
                blocks.append({"type": "image", "source": source})
        return blocks

    async def generate[T: BaseModel](self, *, system: str, parts: Sequence[Part], schema: type[T]) -> T:
        try:
            response = await self._client.beta.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": self._content(parts)}],
                output_format=schema,
                # A safety-classifier decline is re-run server-side on Anthropic's recommended fallback model.
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except pydantic.ValidationError as e:
            raise LLMOutputInvalid(str(e)) from e
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as e:
            # Already retried by the SDK; surface as recoverable so the job retries with backoff.
            raise JobError(ErrorCode.LLM_UNAVAILABLE, f"LLM unavailable: {type(e).__name__}") from e
        except anthropic.APIStatusError as e:
            raise JobError(
                ErrorCode.LLM_REQUEST_FAILED, f"LLM request rejected ({e.status_code}): {e.message}"[:2000]
            ) from e
        except TypeError as e:
            # The SDK signals "no credentials resolvable" with a TypeError; retrying cannot fix configuration.
            if "authentication method" not in str(e):
                raise
            raise JobError(
                ErrorCode.LLM_REQUEST_FAILED,
                "no Anthropic credentials configured (set ANTHROPIC_API_KEY or use LLM_BACKEND=fake)",
            ) from e

        if response.stop_reason == "refusal":
            raise JobError(ErrorCode.LLM_REQUEST_FAILED, "model declined the request (after fallbacks)")
        if response.stop_reason == "max_tokens":
            raise LLMOutputInvalid("output truncated at max_tokens")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMOutputInvalid("no structured output in response")
        return parsed


class FakeLLM:
    """Scripted responses for tests: each entry is a dict (valid JSON) or a raw string."""

    def __init__(self, responses: Sequence[dict[str, Any] | str]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def generate[T: BaseModel](self, *, system: str, parts: Sequence[Part], schema: type[T]) -> T:
        self.calls.append({"system": system, "parts": list(parts), "schema": schema})
        if not self._responses:
            raise AssertionError("FakeLLM ran out of scripted responses")
        raw = self._responses.pop(0)
        text = raw if isinstance(raw, str) else json.dumps(raw)
        try:
            return schema.model_validate_json(text)
        except pydantic.ValidationError as e:
            raise LLMOutputInvalid(str(e)) from e


def create_llm(settings: Settings) -> LLMClient | None:
    if settings.llm_backend == "anthropic":
        return AnthropicLLM(settings)
    return None
