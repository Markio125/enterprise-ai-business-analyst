"""OpenRouter backend, call cap, offline fallback and .env loading, against a fake transport (no network)."""
from __future__ import annotations

import json
import os

import httpx
import pytest

from analyst.llm import FallbackLLM, LLMError, OfflineLLM, OpenRouterLLM, make_llm
from analyst.settings import load_env

SETTINGS = {"llm": {"fallback_to_offline": True, "openrouter": {
    "models_reasoning": ["vendor/big:free", "vendor/backup:free"], "models_fast": ["vendor/small:free"],
    "max_retries": 1, "max_calls": None}}}
SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}


def reply(data, finish="stop", model="vendor/big:free"):
    return {"model": model, "choices": [{"message": {"content": json.dumps(data)}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 20}}}


def make(responses, settings=SETTINGS):
    calls = []

    def handler(req):
        calls.append(req)
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)
    llm = OpenRouterLLM(settings, client=httpx.Client(transport=httpx.MockTransport(handler)), api_key="or-key",
                        sleep=lambda s: None)
    return llm, calls


def test_request_shape_models_and_usage():
    llm, calls = make([(200, reply({"a": 1})), (200, reply({"a": 2}, model="vendor/small:free"))])
    r = llm.json("compose", "SYS", "USER", SCHEMA, tier="reasoning")
    assert r.data == {"a": 1} and r.model == "vendor/big:free"
    assert (r.input_tokens, r.cache_read_tokens, r.output_tokens) == (100, 20, 30)
    req = calls[0]
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer or-key"
    body = json.loads(req.content)
    assert body["model"] == "vendor/big:free" and body["models"] == ["vendor/big:free", "vendor/backup:free"]
    assert body["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]
    assert body["response_format"]["json_schema"] == {"name": "compose", "strict": True, "schema": SCHEMA}
    assert body["provider"] == {"require_parameters": True}       # only providers that honour the schema
    body2 = json.loads((llm.json("parse", "S", "U", SCHEMA, tier="fast"), calls[1].content)[1])
    assert body2["model"] == "vendor/small:free" and "models" not in body2


def test_fenced_json_is_accepted_and_bad_replies_raise():
    fenced = {"model": "m", "choices": [{"message": {"content": "```json\n{\"a\": 5}\n```"}, "finish_reason": "stop"}]}
    llm, _ = make([(200, fenced), (200, reply({"a": 1}, finish="length")),
                   (200, {"choices": [{"message": {"content": "nope"}, "finish_reason": "stop"}]}),
                   (200, {"error": {"message": "no providers"}})])
    assert llm.json("parse", "S", "U", SCHEMA).data == {"a": 5}
    for match in ("cut off", "invalid JSON", "no answer"):
        with pytest.raises(LLMError, match=match):
            llm.json("parse", "S", "U", SCHEMA)


def test_errors_retries_and_the_call_cap_counts_every_request():
    llm, calls = make([(429, {"error": {"message": "rate limited"}}), (200, reply({"a": 1}))])
    assert llm.json("parse", "S", "U", SCHEMA).data == {"a": 1} and len(calls) == 2
    assert llm.budget.used == 2                                    # the retry was a real call too
    llm, _ = make([(401, {"error": {"message": "bad key"}})])
    with pytest.raises(LLMError, match="rejected the key"):
        llm.json("parse", "S", "U", SCHEMA)
    capped = {"llm": {"openrouter": SETTINGS["llm"]["openrouter"] | {"max_calls": 2}}}
    llm, calls = make([(200, reply({"a": 1}))] * 5, settings=capped)
    llm.json("parse", "S", "U", SCHEMA)
    llm.json("parse", "S", "U", SCHEMA)
    with pytest.raises(LLMError, match="call budget used up"):
        llm.json("parse", "S", "U", SCHEMA)
    assert len(calls) == 2                                         # the third request was never sent


def test_env_var_overrides_the_configured_cap(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MAX_CALLS", "10")
    llm, _ = make([])
    assert llm.budget.limit == 10


def test_fallback_answers_from_offline_rules_and_can_be_switched_off():
    class Down:
        name = "openrouter"

        def json(self, *a, **k):
            raise LLMError("rate limit or free quota reached")
    off = OfflineLLM({"parse": lambda ctx: {"ok": True}})
    fb = FallbackLLM(Down(), off)
    r = fb.json("parse", "S", "U", SCHEMA, context={})
    assert r.data == {"ok": True} and r.model == "offline-rules" and fb.fallbacks == 1 and fb.name == "openrouter"
    with pytest.raises(LLMError):
        fb.json("judge", "S", "U", SCHEMA)                         # no offline judge -> the error surfaces
    fb.enabled = False                                             # the eval harness does this
    with pytest.raises(LLMError):
        fb.json("parse", "S", "U", SCHEMA, context={})


def test_factory_wraps_remote_providers_and_respects_the_env_choice(monkeypatch):
    monkeypatch.setattr("analyst.llm.has_anthropic_credentials", lambda: False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("ANALYST_LLM_PROVIDER", "openrouter")
    llm = make_llm(SETTINGS, {"parse": lambda c: {}})
    assert isinstance(llm, FallbackLLM) and isinstance(llm.primary, OpenRouterLLM)
    assert make_llm(SETTINGS, {"parse": lambda c: {}}, "offline").name == "offline"     # argument wins
    monkeypatch.delenv("ANALYST_LLM_PROVIDER")
    assert make_llm(SETTINGS | {"llm": SETTINGS["llm"] | {"provider": "auto"}}, None).name == "openrouter"


def test_load_env_reads_keys_without_overriding(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# demo\nDEMO_KEY_A='abc'\nDEMO_KEY_B=already\n\nbad line\n", encoding="utf-8")
    monkeypatch.delenv("DEMO_KEY_A", raising=False)
    monkeypatch.setenv("DEMO_KEY_B", "set-before")
    assert load_env(f) == ["DEMO_KEY_A"]
    assert os.environ["DEMO_KEY_A"] == "abc" and os.environ["DEMO_KEY_B"] == "set-before"
    monkeypatch.delenv("DEMO_KEY_A")


def test_tests_never_reach_a_real_model():
    assert os.environ.get("ANALYST_LLM_PROVIDER") == "offline" and "OPENROUTER_API_KEY" not in os.environ
