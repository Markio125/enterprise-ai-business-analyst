"""The Gemini backend, against a fake HTTP transport (no network, no key needed)."""
from __future__ import annotations

import json

import httpx
import pytest

from analyst.llm import GeminiLLM, LLMError, make_llm

SETTINGS = {"llm": {"provider": "gemini", "gemini": {"model_reasoning": "gemini-3-flash-preview",
                                                     "model_fast": "gemini-3.1-flash-lite", "max_retries": 3}}}
SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}


def ok_body(data, usage=None, finish="STOP", thought=None):
    parts = ([{"text": thought, "thought": True}] if thought else []) + [{"text": json.dumps(data)}]
    return {"candidates": [{"content": {"parts": parts}, "finishReason": finish}],
            "usageMetadata": usage or {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 30},
            "modelVersion": "x"}


def make(responses, sleeps=None):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)
    slept = sleeps if sleeps is not None else []
    llm = GeminiLLM(SETTINGS, client=httpx.Client(transport=httpx.MockTransport(handler)), api_key="test-key",
                    sleep=slept.append)
    return llm, calls, slept


def test_request_shape_tiers_and_usage():
    llm, calls, _ = make([(200, ok_body({"a": 1}, thought="thinking...")), (200, ok_body({"a": 2}))])
    r = llm.json("parse", "SYSTEM", "USER", SCHEMA, tier="fast")
    assert r.data == {"a": 1} and r.model == "gemini-3.1-flash-lite"
    assert (r.input_tokens, r.output_tokens) == (100, 50)          # thinking tokens billed as output
    req = calls[0]
    assert req.url.path.endswith("/models/gemini-3.1-flash-lite:generateContent")
    assert req.headers["x-goog-api-key"] == "test-key" and "key=" not in str(req.url)   # key never in the URL
    body = json.loads(req.content)
    assert body["systemInstruction"]["parts"][0]["text"] == "SYSTEM"
    assert body["contents"][0]["parts"][0]["text"] == "USER"
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseJsonSchema"] == SCHEMA
    assert llm.json("sql", "S", "U", SCHEMA, tier="reasoning").model == "gemini-3-flash-preview"


def test_cached_tokens_are_split_out():
    llm, _, _ = make([(200, ok_body({"a": 1}, usage={"promptTokenCount": 1000, "cachedContentTokenCount": 600,
                                                       "candidatesTokenCount": 10}))])
    r = llm.json("parse", "S", "U", SCHEMA)
    assert (r.input_tokens, r.cache_read_tokens, r.output_tokens) == (400, 600, 10)


def test_rate_limit_and_overload_are_retried_with_the_server_delay():
    rate = {"error": {"code": 429, "message": "Resource exhausted, per minute",
                      "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]}}
    busy = {"error": {"code": 503, "message": "high demand"}}
    llm, calls, slept = make([(429, rate), (503, busy), (200, ok_body({"a": 3}))])
    assert llm.json("parse", "S", "U", SCHEMA).data == {"a": 3}
    assert len(calls) == 3 and slept[0] == 8.0 and slept[1] > 0


def test_daily_quota_and_non_retryable_errors_fail_at_once():
    daily = {"error": {"code": 429, "message": "Quota exceeded", "details": [
        {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
    llm, calls, slept = make([(429, daily)])
    with pytest.raises(LLMError, match="quota used up"):
        llm.json("parse", "S", "U", SCHEMA)
    assert len(calls) == 1 and not slept
    llm, calls, _ = make([(400, {"error": {"code": 400, "message": "bad schema"}})])
    with pytest.raises(LLMError, match="error 400"):
        llm.json("parse", "S", "U", SCHEMA)
    busy = {"error": {"code": 503, "message": "high demand"}}
    llm, calls, _ = make([(503, busy)] * 4)
    with pytest.raises(LLMError):
        llm.json("parse", "S", "U", SCHEMA)
    assert len(calls) == 4                                             # 1 + max_retries


def test_refusal_truncation_and_bad_json_raise():
    llm, _, _ = make([(200, ok_body({"a": 1}, finish="SAFETY")), (200, ok_body({"a": 1}, finish="MAX_TOKENS")),
                      (200, {"candidates": [], "promptFeedback": {"blockReason": "OTHER"}}),
                      (200, {"candidates": [{"content": {"parts": [{"text": "not json"}]}, "finishReason": "STOP"}]})])
    for match in ("declined", "cut off", "declined", "invalid JSON"):
        with pytest.raises(LLMError, match=match):
            llm.json("parse", "S", "U", SCHEMA)


def test_factory_and_auto_selection(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("analyst.llm.has_anthropic_credentials", lambda: False)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert make_llm(SETTINGS, {}, "gemini").name == "gemini"
    assert make_llm(SETTINGS, {}, "auto").name == "gemini"
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert make_llm(SETTINGS, {}, "auto").name == "offline"
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        make_llm(SETTINGS, {}, "gemini")


# --------------------------------------------------------------------------- what a real model's parse taught us
def _norm(raw, question):
    from analyst.catalog import build_catalog
    from analyst.intent import normalize
    from analyst.policies import Policy
    from analyst.semantic import SemanticModel
    model, pol = SemanticModel.load(), Policy.load()
    return normalize(raw, question, model, build_catalog(model, pol, pol.principal("u_exec")))


GEMINI_PARSE = {   # gemini-3.1-flash-lite's actual parse of the demo question (trimmed)
    "question_type": "DRIVER_ANALYSIS",
    "metrics": [{"term": "payment success rate", "metric": "success_rate", "variant": "attempt_level_excl_user_cancel"}],
    "dimensions": ["failure_category"], "filters": [], "time_phrase": "last two weeks", "compare": "previous_period",
    "custom_request": "Identify the primary failure categories contributing to the drop in success rate.",
}


def test_model_cannot_choose_a_definition_the_user_did_not_name():
    it, _ = _norm(GEMINI_PARSE, "Why did our payment success rate drop in the last two weeks?")
    assert it.metrics == ["success_rate"] and not it.explicit_variants          # -> the clarification is asked
    assert any("doesn't name a definition" in n for n in it.notes)
    raw = GEMINI_PARSE | {"metrics": [{"term": "success rate", "metric": "success_rate", "variant": "order_level"}]}
    it, _ = _norm(raw, "Why did our order-level success rate drop in the last two weeks?")
    assert it.explicit_variants == ["success_rate:order_level"]                 # named by the user -> kept


def test_unasked_breakdowns_and_custom_queries_do_not_replace_the_analysis():
    it, _ = _norm(GEMINI_PARSE, "Why did our payment success rate drop in the last two weeks?")
    assert it.dimensions == [] and it.custom_request is None
    assert any("failure_category" in n for n in it.notes) and any("instead of a custom query" in n for n in it.notes)
    raw = GEMINI_PARSE | {"dimensions": ["hour", "merchant"], "question_type": "TREND"}
    it, _ = _norm(raw, "Show hourly success rate for every merchant for the past year")
    assert it.dimensions == ["hour", "merchant"]                               # mentioned -> kept
    raw = GEMINI_PARSE | {"question_type": "LOOKUP", "custom_request": "median attempts per order before success"}
    it, _ = _norm(raw, "What is the median number of attempts per order before success?")
    assert it.custom_request                                                    # no template for it -> kept


def _raw(**kw):
    return {"question_type": "LOOKUP", "metrics": [], "dimensions": [], "filters": [], "time_phrase": None,
            "compare": "none", "custom_request": None} | kw


SR = [{"term": "success", "metric": "success_rate", "variant": None}]


def test_question_type_guards_follow_the_question_not_the_model():
    it, _ = _norm(_raw(question_type="TREND", metrics=SR, time_phrase="September 10",
                       filters=[{"dimension": "payment_method", "values": ["CARD"]}]),
                  "What happened to card success on September 10?")
    assert it.question_type == "DRIVER_ANALYSIS" and it.compare == "previous_period"
    it, _ = _norm(_raw(question_type="RANKING", metrics=SR, dimensions=["acquirer"]),
                  "Which acquirer had the worst card-not-present performance in late September and why?")
    assert it.question_type == "DRIVER_ANALYSIS" and it.dimensions == ["acquirer"]
    it, _ = _norm(_raw(question_type="TREND", metrics=[{"term": "GMV", "metric": "gmv_inr", "variant": None}],
                       dimensions=["merchant_segment"]), "GMV by merchant segment for Oct 18-20")
    assert it.question_type == "DISTRIBUTION"
    it, _ = _norm(_raw(question_type="TREND", metrics=SR), "How is success rate trending lately?")
    assert it.question_type == "TREND"                                          # a real trend stays a trend


def test_reconciliation_gets_both_sources_even_when_the_model_lists_none():
    it, _ = _norm(_raw(question_type="RECONCILIATION", custom_request="Explain the methodological differences"),
                  "Why does finance's settled amount for October differ from the dashboard?")
    assert set(it.metrics) == {"gmv_inr", "finance_gross_inr", "settled_gmv_inr"} and it.custom_request is None


def test_out_of_scope_region_is_kept_so_the_access_check_refuses_it():
    from analyst.catalog import build_catalog
    from analyst.intent import normalize
    from analyst.policies import Policy
    from analyst.semantic import SemanticModel
    model, pol = SemanticModel.load(), Policy.load()
    cat = build_catalog(model, pol, pol.principal("u_west"))
    gmv = [{"term": "GMV", "metric": "gmv_inr", "variant": None}]
    q = "GMV for the NORTH region last month"
    for filters in ([{"dimension": "region", "values": ["NORTH"]}], []):      # model kept it, or dropped it
        it, problems = normalize(_raw(metrics=gmv, filters=filters), q, model, cat)
        assert {"dimension": "region", "op": "=", "values": ["NORTH"]} in it.filters and not problems
    it, _ = normalize(_raw(metrics=gmv), "GMV region wise last month", model, cat)
    assert not it.filters


def test_grain_is_kept_only_when_the_question_asks_for_one():
    gmv = [{"term": "GMV", "metric": "gmv_inr", "variant": None}]
    it, _ = _norm(_raw(question_type="TREND", metrics=gmv, dimensions=["merchant_segment"]) | {"grain": "day"},
                  "GMV by merchant segment for Oct 18-20")
    assert it.grain == "none" and it.question_type == "DISTRIBUTION"
    it, _ = _norm(_raw(question_type="TREND", metrics=gmv) | {"grain": "week"}, "Weekly GMV for the last quarter")
    assert it.grain == "week" and it.question_type == "TREND"
    it, _ = _norm(_raw(question_type="TREND", metrics=SR, dimensions=["hour", "merchant"]) | {"grain": "hour"},
                  "Show hourly success rate for every merchant for the past year")
    assert it.grain == "hour"
