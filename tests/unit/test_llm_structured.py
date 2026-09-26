import base64
import logging
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from app.ai.llm import AnthropicLLM, FakeLLM, ImagePart, LLMOutputInvalid, TextPart, structured
from app.config import Settings
from app.jobs.errors import ErrorCode, JobError, Recoverability, recoverability
from app.schemas.analysis import SceneAnalysis

VALID = {
    "overall_summary": "A short demo.",
    "scenes": [{"segment_index": 0, "summary": "Title card", "relevance": 0.4}],
}


async def test_valid_output_is_parsed():
    llm = FakeLLM([VALID])
    out = await structured(llm, system="s", parts=[TextPart("go")], schema=SceneAnalysis)
    assert out.scenes[0].relevance == 0.4
    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    "bad",
    [
        '{"overall_summary": "missing scenes"}',
        '{"overall_summary": "x", "scenes": [{"segment_index": "zero", "summary": "s", "relevance": 0.5}]}',
        '{"overall_summary": "x", "scenes": [{"segment_index": 0, "summary": "s", "relevance": 1.7}]}',
        "this is not json",
    ],
)
async def test_one_repair_round_includes_the_validation_error(bad):
    llm = FakeLLM([bad, VALID])
    out = await structured(llm, system="s", parts=[TextPart("go")], schema=SceneAnalysis)
    assert out.overall_summary == "A short demo."
    assert len(llm.calls) == 2
    repair_text = llm.calls[1]["parts"][-1].text
    assert "did not match the required schema" in repair_text


async def test_two_invalid_outputs_fail_the_job_fatally():
    llm = FakeLLM(["nope", '{"also": "wrong"}'])
    with pytest.raises(JobError) as exc:
        await structured(llm, system="s", parts=[TextPart("go")], schema=SceneAnalysis)
    assert exc.value.code is ErrorCode.LLM_OUTPUT_INVALID
    assert recoverability(exc.value.code) is Recoverability.FATAL


def _settings(**kw) -> Settings:
    return Settings(llm_model="claude-opus-5", anthropic_api_key=SecretStr("sk-test-secret"), **kw)


class StubMessages:
    def __init__(self, result=None, exc=None):
        self.kwargs = None
        self.result, self.exc = result, exc

    async def parse(self, **kwargs):
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.result


def _stub_client(messages: StubMessages):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


def _response(parsed, stop_reason="end_turn"):
    return SimpleNamespace(
        parsed_output=parsed, stop_reason=stop_reason, stop_details=None, _request_id="req_1"
    )


async def test_anthropic_request_shape():
    msgs = StubMessages(_response(SceneAnalysis.model_validate(VALID)))
    llm = AnthropicLLM(_settings(), client=_stub_client(msgs))
    jpeg = b"\xff\xd8\xff\xe0fakejpeg"

    out = await llm.generate(
        system="sys", parts=[TextPart("segment 0"), ImagePart(jpeg)], schema=SceneAnalysis
    )

    assert out.overall_summary == "A short demo."
    kw = msgs.kwargs
    assert kw["model"] == "claude-opus-5"
    assert kw["output_format"] is SceneAnalysis
    assert kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["system"] == "sys"
    content = kw["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "segment 0"}
    assert content[1]["source"] == {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": base64.standard_b64encode(jpeg).decode(),
    }


async def test_anthropic_validation_error_becomes_output_invalid():
    try:
        SceneAnalysis.model_validate({"overall_summary": "x"})
    except Exception as e:
        validation_error = e
    llm = AnthropicLLM(_settings(), client=_stub_client(StubMessages(exc=validation_error)))
    with pytest.raises(LLMOutputInvalid):
        await llm.generate(system="s", parts=[TextPart("t")], schema=SceneAnalysis)


async def test_anthropic_refusal_is_fatal():
    llm = AnthropicLLM(_settings(), client=_stub_client(StubMessages(_response(None, "refusal"))))
    with pytest.raises(JobError) as exc:
        await llm.generate(system="s", parts=[TextPart("t")], schema=SceneAnalysis)
    assert exc.value.code is ErrorCode.LLM_REQUEST_FAILED


async def test_anthropic_connection_error_is_retryable():
    err = anthropic.APIConnectionError(
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    llm = AnthropicLLM(_settings(), client=_stub_client(StubMessages(exc=err)))
    with pytest.raises(JobError) as exc:
        await llm.generate(system="s", parts=[TextPart("t")], schema=SceneAnalysis)
    assert exc.value.code is ErrorCode.LLM_UNAVAILABLE
    assert recoverability(exc.value.code) is Recoverability.RETRY


async def test_api_key_never_appears_in_logs(caplog):
    msgs = StubMessages(_response(SceneAnalysis.model_validate(VALID)))
    llm = AnthropicLLM(_settings(), client=_stub_client(msgs))
    with caplog.at_level(logging.DEBUG):
        await llm.generate(system="s", parts=[TextPart("t")], schema=SceneAnalysis)
    assert "sk-test-secret" not in caplog.text
    assert "sk-test-secret" not in repr(llm)


async def test_missing_credentials_fail_fast_instead_of_retrying():
    # Exact error the SDK raises when no api_key / auth_token / profile can be resolved (observed live).
    err = TypeError(
        '"Could not resolve authentication method. '
        "Expected one of api_key, auth_token, or credentials to be set."
    )
    llm = AnthropicLLM(_settings(), client=_stub_client(StubMessages(exc=err)))
    with pytest.raises(JobError) as exc:
        await llm.generate(system="s", parts=[TextPart("t")], schema=SceneAnalysis)
    assert exc.value.code is ErrorCode.LLM_REQUEST_FAILED
    assert "credentials" in exc.value.message
