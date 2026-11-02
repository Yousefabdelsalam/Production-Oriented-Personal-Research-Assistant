"""The real OpenAI-compatible HTTP client, driven through httpx.MockTransport (real client code, fake server).

These reproduce responses actually observed from api.openai.com for a GPT-5-family model. A live check against the real
API is done separately and is not part of the offline suite.
"""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from app.config import Settings
from app.errors import RetryableError
from app.services.llm import LLMError, OpenAICompatibleEmbedder, OpenAICompatibleLLM


class Pong(BaseModel):
    answer: str


def _ok(content='{"answer": "pong"}'):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}})


def _err(status, message):
    return httpx.Response(status, json={"error": {"message": message}})


def _client(handler, **kw):
    return OpenAICompatibleLLM("https://api.example/v1/", "sk-test-key", kw.pop("model", "m"), transport=httpx.MockTransport(handler),
                               backoff_s=0.0, **kw)


def _call(c):
    return c.complete_json(purpose="t", system="s", user="u", schema=Pong, max_tokens=100)


def test_plain_request_uses_classic_parameters_and_authorization_header():
    seen = []

    def handler(req):
        seen.append((dict(req.headers), json.loads(req.content)))
        return _ok()

    out, usage = _call(_client(handler))
    assert out.answer == "pong" and (usage.input_tokens, usage.output_tokens) == (7, 3)
    headers, body = seen[0]
    assert headers["authorization"] == "Bearer sk-test-key"
    assert body["max_tokens"] == 100 and body["temperature"] == 0 and "max_completion_tokens" not in body


def test_model_name_and_credentials_are_stripped():
    seen = []
    c = OpenAICompatibleLLM(" https://api.example/v1/ ", " sk-x ", "gpt-5-mini ", transport=httpx.MockTransport(lambda r: (seen.append(json.loads(r.content)), _ok())[1]))
    _call(c)
    assert seen[0]["model"] == "gpt-5-mini" and c.base_url == "https://api.example/v1" and c.api_key == "sk-x"


def test_settings_strip_whitespace_inside_quoted_env_values(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gpt-5-mini ")
    monkeypatch.setenv("LLM_API_KEY", " sk-abc ")
    s = Settings(_env_file=None, data_dir=tmp_path)
    assert s.llm_model == "gpt-5-mini" and s.llm_api_key == "sk-abc"


def test_adapts_to_models_that_reject_max_tokens_and_temperature_and_remembers_it():
    bodies = []

    def handler(req):
        b = json.loads(req.content)
        bodies.append(b)
        if "max_tokens" in b:
            return _err(400, "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.")
        if "temperature" in b:
            return _err(400, "Unsupported value: 'temperature' does not support 0 with this model. Only the default (1) value is supported.")
        return _ok()

    c = _client(handler)
    out, _ = _call(c)
    assert out.answer == "pong"
    assert len(bodies) == 3  # classic form -> completion-tokens form -> without temperature
    final = bodies[-1]
    assert "max_tokens" not in final and "temperature" not in final
    assert final["max_completion_tokens"] == 100 + OpenAICompatibleLLM.REASONING_HEADROOM  # room for hidden reasoning tokens
    _call(c)
    assert len(bodies) == 4 and "temperature" not in bodies[-1]  # remembered: no more probing requests


def test_unrelated_400_is_not_retried_and_does_not_leak_the_response_body():
    calls = []

    def handler(req):
        calls.append(1)
        return _err(400, "invalid model ID (your prompt text SECRET-PROMPT-TEXT was rejected)")

    with pytest.raises(LLMError) as ei:
        _call(_client(handler))
    assert len(calls) == 1 and "SECRET-PROMPT-TEXT" not in str(ei.value) and "400" in str(ei.value)


def test_auth_failure_is_a_clear_non_retryable_error_without_key_in_message():
    calls = []
    with pytest.raises(LLMError) as ei:
        _call(_client(lambda r: (calls.append(1), _err(401, "bad key sk-test-key"))[1]))
    assert len(calls) == 1 and "sk-test-key" not in str(ei.value) and "401" in str(ei.value)


def test_rate_limit_and_server_errors_are_retried_then_reported_retryable():
    calls = []
    c = _client(lambda r: (calls.append(1), httpx.Response(429))[1], max_retries=2)
    with pytest.raises(RetryableError):
        _call(c)
    assert len(calls) == 3
    n = []
    c2 = _client(lambda r: (n.append(1), httpx.Response(503) if len(n) < 2 else _ok())[1])
    assert _call(c2)[0].answer == "pong"  # recovers


def test_timeouts_are_retryable():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(RetryableError):
        _call(_client(handler, max_retries=1))


def test_invalid_json_from_model_is_repaired_once_then_rejected():
    replies = iter(["not json", '{"answer": "fixed"}'])
    out, usage = _call(_client(lambda r: _ok(next(replies))))
    assert out.answer == "fixed" and usage.attempts == 2
    with pytest.raises(LLMError, match="schema validation"):
        _call(_client(lambda r: _ok('{"wrong": 1}')))


def test_embedder_batches_orders_results_and_classifies_errors():
    calls = []

    def handler(req):
        inputs = json.loads(req.content)["input"]
        calls.append(len(inputs))
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [float(i), 0.0]} for i in reversed(range(len(inputs)))]})

    e = OpenAICompatibleEmbedder("https://api.example/v1", "k", " text-embedding-3-small ", transport=httpx.MockTransport(handler))
    vecs = e.embed([f"t{i}" for i in range(70)])
    assert calls == [64, 6] and [v[0] for v in vecs[:3]] == [0.0, 1.0, 2.0] and e.model == "text-embedding-3-small"
    with pytest.raises(RetryableError):
        OpenAICompatibleEmbedder("https://api.example/v1", "k", "m", transport=httpx.MockTransport(lambda r: httpx.Response(500))).embed(["x"])
    with pytest.raises(LLMError):
        OpenAICompatibleEmbedder("https://api.example/v1", "k", "m", transport=httpx.MockTransport(lambda r: httpx.Response(401))).embed(["x"])
