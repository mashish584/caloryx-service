"""T2 provider integration - PRD §7.3. Mocks the OpenAI SDK call boundary
(not the network): `llm.client` is the only module that imports `openai`,
so patching `openai.OpenAI`/`openai.OpenAIError` here is enough to exercise
every branch without a real API key or a live call.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.test import override_settings

from llm import LLMCallError, LLMConfigurationError, call_large_model, call_small_model
from llm.client import reset_cache


@pytest.fixture(autouse=True)
def _reset_client_cache():
    reset_cache()
    yield
    reset_cache()


def _fake_response(content, prompt_tokens=10, output_tokens=5, model="gpt-4o-mini"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=output_tokens),
        model=model,
    )


def _envelope_json():
    return json.dumps(
        {
            "intent": "LOG_NEW",
            "targetRef": None,
            "slot": None,
            "mealName": None,
            "items": [
                {
                    "food": "grilled chicken salad",
                    "quantity": None,
                    "unit": None,
                    "state": None,
                    "prep": None,
                    "sizeQualifier": None,
                    "confidence": 0.7,
                }
            ],
        }
    )


class _FakeCompletions:
    def __init__(self, response=None, exc=None, calls=None):
        self._response = response
        self._exc = exc
        self._calls = calls if calls is not None else []

    def create(self, **kwargs):
        self._calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._response


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeOpenAI:
    def __init__(self, calls, response=None, exc=None, **client_kwargs):
        self.client_kwargs = client_kwargs
        self.completions = _FakeCompletions(response=response, exc=exc, calls=calls)
        self.chat = _FakeChat(self.completions)


@override_settings(OPENAI_API_KEY="sk-test", OPENAI_SMALL_MODEL="gpt-4o-mini", OPENAI_MAX_TOKENS=300)
def test_call_small_model_sends_expected_request_shape(monkeypatch):
    import openai

    calls = []
    monkeypatch.setattr(
        openai, "OpenAI", lambda **kw: _FakeOpenAI(calls, response=_fake_response(_envelope_json()), **kw)
    )

    call_small_model("system prompt text", "200g something descriptive")

    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["model"] == "gpt-4o-mini"
    assert kwargs["max_tokens"] == 300
    assert kwargs["messages"] == [
        {"role": "system", "content": "system prompt text"},
        {"role": "user", "content": "200g something descriptive"},
    ]
    assert kwargs["response_format"]["type"] == "json_schema"
    assert kwargs["response_format"]["json_schema"]["strict"] is True
    assert kwargs["response_format"]["json_schema"]["name"] == "intent_envelope"


@override_settings(OPENAI_API_KEY="sk-test")
def test_call_small_model_parses_a_successful_response_into_llmresponse(monkeypatch):
    import openai

    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda **kw: _FakeOpenAI(
            [], response=_fake_response(_envelope_json(), prompt_tokens=42, output_tokens=13, model="gpt-4o-mini-2024")
        ),
    )

    result = call_small_model("sys", "some content")

    assert result.raw_envelope["intent"] == "LOG_NEW"
    assert result.raw_envelope["items"][0]["food"] == "grilled chicken salad"
    assert result.prompt_tokens == 42
    assert result.output_tokens == 13
    assert result.model == "gpt-4o-mini-2024"
    assert result.latency_ms >= 0


@override_settings(OPENAI_API_KEY="sk-test")
def test_call_small_model_raises_llmcallerror_on_a_provider_error(monkeypatch):
    import openai

    class _Boom(openai.OpenAIError):
        pass

    monkeypatch.setattr(openai, "OpenAI", lambda **kw: _FakeOpenAI([], exc=_Boom("rate limited")))

    with pytest.raises(LLMCallError):
        call_small_model("sys", "some content")


@override_settings(OPENAI_API_KEY="sk-test")
def test_call_small_model_raises_llmcallerror_on_non_json_content(monkeypatch):
    import openai

    monkeypatch.setattr(
        openai, "OpenAI", lambda **kw: _FakeOpenAI([], response=_fake_response("not json"))
    )

    with pytest.raises(LLMCallError):
        call_small_model("sys", "some content")


@override_settings(OPENAI_API_KEY="sk-test")
def test_non_json_content_is_never_logged_verbatim(monkeypatch, caplog):
    """Log hygiene (§12.14, Chunk 8c) - the model's raw response body can echo
    back whatever the user said, so only a length and a hash may be logged."""
    import openai

    raw_body = "not json, and definitely not john.doe@example.com either"
    monkeypatch.setattr(
        openai, "OpenAI", lambda **kw: _FakeOpenAI([], response=_fake_response(raw_body))
    )

    with caplog.at_level("WARNING"):
        with pytest.raises(LLMCallError):
            call_small_model("sys", "some content")

    logged_text = "\n".join(record.getMessage() for record in caplog.records)
    assert raw_body not in logged_text
    assert "john.doe@example.com" not in logged_text
    assert "len={}".format(len(raw_body)) in logged_text


@override_settings(OPENAI_API_KEY="")
def test_missing_api_key_raises_llmconfigurationerror():
    with pytest.raises(LLMConfigurationError):
        call_small_model("sys", "some content")


@override_settings(OPENAI_API_KEY="sk-test", OPENAI_LARGE_MODEL="gpt-4o")
def test_call_large_model_sends_the_large_model_name(monkeypatch):
    import openai

    calls = []
    monkeypatch.setattr(
        openai, "OpenAI", lambda **kw: _FakeOpenAI(calls, response=_fake_response(_envelope_json()))
    )

    result = call_large_model("system prompt text", "some content")

    assert len(calls) == 1
    assert calls[0]["model"] == "gpt-4o"
    assert result.raw_envelope["intent"] == "LOG_NEW"
