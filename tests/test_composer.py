"""Segment 6: answer composition, number grounding, confidence, charts."""
from __future__ import annotations

import shutil

import pytest

from analyst.composer import Answer, allowed_numbers, check_rules, fmt, ground
from analyst.dq import DQResult
from analyst.executor import QueryGateway
from analyst.llm import LLMError, OfflineLLM, ScriptedLLM
from analyst.pipeline import OFFLINE_HANDLERS, Analyst, Session

DRIVER_Q = "Why did success rate drop in the last two weeks?"
CLARIFIED = {"METRIC_DEFINITION:success_rate": "a"}


@pytest.fixture(scope="module")
def gateway(small_db, tmp_path_factory):
    path = tmp_path_factory.mktemp("cp") / "composer.duckdb"
    shutil.copy(small_db[0], path)
    g = QueryGateway(db_path=path)
    yield g
    g.close()


@pytest.fixture(scope="module")
def analyst(gateway):
    return Analyst(gateway=gateway, llm=OfflineLLM(OFFLINE_HANDLERS))


@pytest.fixture(scope="module")
def driver(analyst):
    return analyst.ask(DRIVER_Q, "u_exec", Session("u_exec", clarifications=dict(CLARIFIED)))


# ----------------------------------------------------------------------------- formatting + grounding
def test_formats():
    assert fmt(0.8549, "percent") == "85.5%"
    assert fmt(621667538.41, "currency_inr") == "₹62.17 Cr"
    assert fmt(7312974.56, "currency_inr") == "₹73.1 L"
    assert fmt(4909, "currency_inr") == "₹4,909"
    assert fmt(-1e-7, "currency_inr") == "₹0"
    assert fmt(8189, "integer") == "8,189"


def _ans(*facts, headline="h"):
    return Answer("ANSWERED", headline, facts=[{"id": f"F{i}", "text": t, "evidence": ["s1"]}
                                               for i, t in enumerate(facts, 1)])


def test_grounding_matches_formatted_values_and_flags_inventions():
    allowed = sorted({0.8813, 0.8549, -2.6345, 621667538.41, 8189.0, 88.13, 85.49})
    ok = ground(_ans("SR 88.1% → 85.5% (-2.63pp) on Oct 14–27, 2026-10-05 at 18:00.", "GMV ₹62.17 Cr; 8,189 txns."),
                allowed)
    assert ok["passed"] and ok["checked"] == 5
    bad = ground(_ans("SR fell to 81.2%.", "GMV ₹70.00 Cr."), allowed)
    assert not bad["passed"] and {u["where"] for u in bad["unmatched"]} == {"fact F1", "fact F2"}


def test_grounding_ignores_dates_times_ids_and_small_counts():
    g = ground(_ans("Top 5 merchants on Sep 10, 18:00-23:00 IST, MID100022, P0 vs P1, in 2026."), [])
    assert g["passed"] and g["checked"] == 0


# ----------------------------------------------------------------------------- the draft
def test_driver_answer_contract(driver):
    a = driver.answer
    assert driver.state == "ANSWERED" and a.status == "ANSWERED"
    assert a.grounding["passed"] and a.grounding["checked"] >= 10
    assert not check_rules(a)
    assert {"metric", "time_window", "scope", "data_version"} <= set(a.definitions_used)
    assert all(f["evidence"] for f in a.facts)
    step_ids = {s.step_id for s in driver.steps}
    assert all(e in step_ids for f in a.facts for e in f["evidence"])
    assert all(h["how_to_validate"] and h["confidence"] for h in a.hypotheses)
    assert all(r["based_on"] for r in a.recommendations)
    assert "mix" in a.to_markdown().lower()
    assert a.confidence["level"] in ("HIGH", "MEDIUM") and a.confidence["reasons"]
    types = {c["type"] for c in a.charts}
    assert {"kpi", "line", "waterfall"} <= types
    wf = next(c for c in a.charts if c["type"] == "waterfall")
    assert abs(wf["data"][0]["value"] + wf["data"][1]["delta"] + wf["data"][2]["delta"] - wf["data"][3]["value"]) < 1e-9


def test_every_state_has_an_answer(analyst):
    for q, u in (("Delete duplicate transactions from October 5", "u_exec"),
                 ("Show me MDR revenue by merchant last month", "u_ops"),
                 ("What's our success rate last week?", "u_exec"),
                 ("What is our NPS?", "u_exec")):
        r = analyst.ask(q, u)
        assert r.answer is not None and r.answer.headline and r.answer.status == r.state
    r = analyst.ask("What's our success rate last week?", "u_exec")
    assert r.state == "AWAITING_CLARIFICATION" and len(r.answer.suggested_follow_ups) >= 3


def test_assumed_interpretation_becomes_a_caveat(gateway):
    a = Analyst(gateway=gateway, llm=OfflineLLM(OFFLINE_HANDLERS))
    a.detector.mode = "assume_and_disclose"
    r = a.ask("What's our success rate last week?", "u_exec")
    assert any(c["code"] == "ASSUMED" and c["text"].startswith("I assumed") for c in r.answer.caveats)
    assert "Interpretation assumed, not confirmed" in r.answer.confidence["limitations"]


# ----------------------------------------------------------------------------- LLM wording + grounding loop
def _rewrite(answer: Answer, change=lambda t: t, headline=None):
    return {"headline": headline or answer.headline,
            "facts": [{"id": f["id"], "text": change(f["text"])} for f in answer.facts],
            "hypotheses": [{"id": h["id"], "text": h["text"]} for h in answer.hypotheses],
            "recommendations": [{"id": r["id"], "text": r["text"]} for r in answer.recommendations]}


def _scripted(gateway, driver, replies):
    llm = ScriptedLLM({"compose": replies}, fallback=OfflineLLM(OFFLINE_HANDLERS))
    return Analyst(gateway=gateway, llm=llm), llm


def test_grounded_rewrite_is_accepted(gateway, driver):
    good = _rewrite(driver.answer, headline="Success rate slipped, mostly from a traffic mix shift.")
    a, llm = _scripted(gateway, driver, [good])
    r = a.ask(DRIVER_Q, "u_exec", Session("u_exec", clarifications=dict(CLARIFIED)))
    assert r.answer.composed_by == "scripted" and r.answer.headline.startswith("Success rate slipped")
    assert r.state == "ANSWERED"
    assert [c["tier"] for c in llm.calls if c["task"] == "compose"] == ["reasoning"]


def test_invented_number_is_sent_back_then_fixed(gateway, driver):
    bad = _rewrite(driver.answer, headline="Success rate fell 7.77pp because of mix.")
    good = _rewrite(driver.answer, headline="Success rate fell because of a mix shift.")
    a, llm = _scripted(gateway, driver, [bad, good])
    r = a.ask(DRIVER_Q, "u_exec", Session("u_exec", clarifications=dict(CLARIFIED)))
    calls = [c for c in llm.calls if c["task"] == "compose"]
    assert len(calls) == 2 and "7.77" in calls[1]["user"]
    assert r.answer.headline == "Success rate fell because of a mix shift." and r.answer.grounding["attempts"] == 2


def test_persistent_hallucination_falls_back_to_the_draft(gateway, driver):
    bad = _rewrite(driver.answer, change=lambda t: t + " Roughly 99.9% of merchants agree.")
    a, _ = _scripted(gateway, driver, [bad, bad, bad])
    r = a.ask(DRIVER_Q, "u_exec", Session("u_exec", clarifications=dict(CLARIFIED)))
    assert r.answer.composed_by == "draft" and r.answer.grounding["rewrite_failed"]
    assert "99.9" not in r.answer.to_markdown()
    assert r.answer.confidence["score"] < driver.answer.confidence["score"]
    assert "Wording could not be verified; showing the plain draft" in r.answer.confidence["limitations"]


def test_rewrite_that_changes_ids_is_rejected(gateway, driver):
    wrong = _rewrite(driver.answer)
    wrong["facts"] = wrong["facts"][:-1]
    a, _ = _scripted(gateway, driver, [wrong, wrong, wrong])
    r = a.ask(DRIVER_Q, "u_exec", Session("u_exec", clarifications=dict(CLARIFIED)))
    assert r.answer.composed_by == "draft" and len(r.answer.facts) == len(driver.answer.facts)


def test_llm_outage_keeps_the_draft(gateway, driver):
    a, _ = _scripted(gateway, driver, [LLMError("provider down")])
    r = a.ask(DRIVER_Q, "u_exec", Session("u_exec", clarifications=dict(CLARIFIED)))
    assert r.state == "ANSWERED" and r.answer.composed_by == "draft" and r.answer.grounding["passed"]


# ----------------------------------------------------------------------------- data-quality block
def test_blocking_dq_issue_changes_the_headline(analyst, monkeypatch):
    real = analyst.dq.run

    def with_block(start, end, dims=None, tables=None):
        return real(start, end, dims, tables) + [DQResult(
            "DQ02_NULL_KEYS", "v_attempts", [str(start), str(end)], "BLOCK", 0.4, 0.10,
            "40% of attempts have no merchant_key.", affects=["merchant_segment"])]
    monkeypatch.setattr(analyst.dq, "run", with_block)
    r = analyst.ask("GMV by merchant segment last week", "u_exec")
    assert r.state == "ANSWERED_WITH_DQ_BLOCK" and r.answer.headline.startswith("I can't give a reliable answer")
    assert r.answer.facts                         # still says what it can
    assert r.answer.confidence["level"] in ("LOW", "MEDIUM")


def test_allowed_numbers_include_results_and_analysis(driver):
    vals = allowed_numbers(driver)
    assert any(abs(v - driver.analysis.headline["P1"]) < 1e-12 for v in vals)
    assert any(abs(v - driver.analysis.headline["P1"] * 100) < 1e-9 for v in vals)
