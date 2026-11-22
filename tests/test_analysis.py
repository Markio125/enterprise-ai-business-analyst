"""Segment 5: analysis engine, data-quality checks, reconciliation, and two gate fixes found while building it."""
from __future__ import annotations

import random
import shutil
from datetime import date

import pandas as pd
import pytest

from analyst.analysis import contribution, decompose_rate, two_prop_p
from analyst.data_checks import mix_rate_decomposition
from analyst.executor import QueryGateway
from analyst.llm import OfflineLLM
from analyst.pipeline import OFFLINE_HANDLERS, Analyst, Session
from analyst.semantic import Filter, QuerySpec, SemanticModel, SemanticError
from analyst.sqlsafety import bind_params

ANSWERED = ("ANSWERED", "ANSWERED_LOW_CONFIDENCE")


# ----------------------------------------------------------------------------- pure methods
def _random_parts(rng, segs=6):
    out = {}
    for i in range(segs):
        den = rng.randint(50, 5000)
        out[f"s{i}"] = (rng.randint(0, den), den)
    return out


@pytest.mark.parametrize("seed", range(20))
def test_rate_decomposition_is_exact(seed):
    rng = random.Random(seed)
    p0, p1 = _random_parts(rng), _random_parts(rng)
    if seed % 3 == 0:                       # a new and a disappearing segment
        p1["new"] = (300, 400)
        p0["gone"] = (10, 90)
    d = decompose_rate("x", p0, p1, 500, 0.05)
    assert abs(d.totals["residual_pp"]) < 1e-9
    assert abs(sum(s.total_pp for s in d.segments) - d.overall["delta_pp"]) < 1e-9
    assert any(s.new_or_gone for s in d.segments) == (seed % 3 == 0)


def test_totals_match_the_ground_truth_decomposition():
    rng = random.Random(7)
    p0, p1 = _random_parts(rng), _random_parts(rng)
    df = pd.DataFrame([{"period": per, "segment": k, "succ": v[0], "denom": v[1]}
                       for per, parts in (("P0", p0), ("P1", p1)) for k, v in parts.items()])
    _, tot = mix_rate_decomposition(df)
    d = decompose_rate("segment", p0, p1, 500, 0.05)
    assert abs(d.totals["mix_effect_pp"] - tot["mix_effect_pp"]) < 1e-9
    assert abs(d.totals["rate_effect_pp"] - tot["rate_effect_pp"]) < 1e-9


def test_centred_mix_effect_is_interpretable_per_segment():
    # a low-rate segment triples its share; every segment's own rate is unchanged
    p0 = {"good": (900, 1000), "bad": (70, 100)}
    p1 = {"good": (900, 1000), "bad": (350, 500)}
    d = decompose_rate("x", p0, p1, 50, 0.05)
    bad = next(s for s in d.segments if s.segment == "bad")
    good = next(s for s in d.segments if s.segment == "good")
    assert d.interpretation_code == "MIX_DOMINANT"
    assert bad.mix_effect_pp < 0 and abs(bad.rate_effect_pp) < 1e-9     # growing below-average segment drags
    assert good.total_pp < 0 and abs(bad.total_pp) > abs(good.total_pp)
    assert d.segments[0].segment == "bad" and d.quality > 0.9


def test_no_significant_change_and_rate_dominant():
    flat = decompose_rate("x", {"a": (850, 1000)}, {"a": (845, 1000)}, 50, 0.05)
    assert flat.interpretation_code == "NO_SIGNIFICANT_CHANGE"
    drop = decompose_rate("x", {"a": (9000, 10000), "b": (9000, 10000)},
                          {"a": (6000, 10000), "b": (9000, 10000)}, 50, 0.05)
    assert drop.interpretation_code == "RATE_DOMINANT" and drop.segments[0].segment == "a"


def test_contribution_for_additive_metrics():
    c = contribution("x", {"a": 100.0, "b": 50.0}, {"a": 160.0, "b": 40.0})
    assert c.overall["delta"] == 50 and abs(sum(s["delta"] for s in c.segments) - 50) < 1e-9
    assert c.segments[0]["segment"] == "a" and c.interpretation_code == "CONCENTRATED"


def test_two_proportion_test():
    assert two_prop_p(500, 1000, 500, 1000) == pytest.approx(1.0)
    assert two_prop_p(400, 1000, 600, 1000) < 1e-10
    assert two_prop_p(1, 0, 1, 10) is None


# ----------------------------------------------------------------------------- semantic parts + gate fixes
def test_ratio_parts_resolve_and_complements_stay_exact():
    m = SemanticModel.load()
    num = m.resolve_metric("success_rate:attempt_level_excl_user_cancel#num")
    den = m.resolve_metric("success_rate:attempt_level_excl_user_cancel#den")
    assert num.ref.endswith("#num") and "SUCCESS" in num.sql and "USER_CANCELLED" in den.sql
    fnum = m.resolve_metric("failure_rate#num")
    assert "SUCCESS" in fnum.sql and "USER_CANCELLED" in fnum.sql and " - " in fnum.sql
    assert m.is_ratio("success_rate") and m.is_ratio("failure_rate:order_level") and not m.is_ratio("gmv_inr")
    with pytest.raises(SemanticError):
        m.resolve_metric("gmv_inr#num")


def test_bind_params_follows_text_order_with_nested_conditions():
    sql = "SELECT 1 FROM t WHERE a = ? AND b IN (?, ?) AND (c = ? OR d = ?) AND e BETWEEN ? AND ?"
    out = bind_params(sql, [1, "x", "y", "c", "d", 5, 6])
    assert out == ("SELECT 1 FROM t WHERE a = 1 AND b IN ('x', 'y') AND (c = 'c' OR d = 'd') "
                   "AND e BETWEEN 5 AND 6")


@pytest.fixture(scope="module")
def gateway(small_db, tmp_path_factory):
    path = tmp_path_factory.mktemp("an") / "analysis.duckdb"
    shutil.copy(small_db[0], path)
    g = QueryGateway(db_path=path)
    yield g
    g.close()


def test_semantic_filters_bind_to_the_right_columns(gateway):
    p = gateway.policy.principal("u_exec")
    win = (date(2026, 9, 1), date(2026, 9, 30))
    spec = QuerySpec(metrics=["attempt_count"], filters=[Filter("payment_method", "=", "CARD"),
                                                         Filter("status", "in", ["FAILED", "TIMEOUT"]),
                                                         Filter("device_type", "=", "WEB")], time_range=win)
    got = gateway.run_spec(spec, p).rows[0][0]
    want = gateway.executor.con.execute(
        """SELECT COUNT(*) FROM v_attempts a JOIN dim_payment_method pm USING (payment_method_key)
           WHERE pm.method = 'CARD' AND a.status IN ('FAILED','TIMEOUT') AND a.device_type = 'WEB'
           AND a.date_key BETWEEN 20260901 AND 20260930""").fetchone()[0]
    assert got == want > 0


def test_warnings_do_not_skip_rls_or_block_execution(gateway):
    p = gateway.policy.principal("u_west")
    # hand-written metric SQL that differs from the semantic layer -> S14 warning only
    sql = ("SELECT COUNT(*) FILTER (WHERE status = 'SUCCESS') * 1.0 / NULLIF(COUNT(*), 0) AS sr FROM v_attempts "
           "WHERE date_key BETWEEN 20261001 AND 20261027")
    v = gateway.validate(sql, p, metrics_used=["success_rate:attempt_level_excl_user_cancel"])
    assert v.ok and v.warnings and v.rewritten_sql and "WEST" in v.rewritten_sql
    r = gateway.executor.execute(v)
    assert r.ok


# ----------------------------------------------------------------------------- end to end on the small warehouse
@pytest.fixture(scope="module")
def analyst(gateway):
    return Analyst(gateway=gateway, llm=OfflineLLM(OFFLINE_HANDLERS))


def test_driver_analysis_end_to_end(analyst):
    s = Session("u_exec", clarifications={"METRIC_DEFINITION:success_rate": "a"})
    r = analyst.ask("Why did success rate drop in the last two weeks?", "u_exec", s)
    a = r.analysis
    assert r.state in ANSWERED and a is not None
    assert a.headline["delta_pp"] < 0 and a.headline["significant"]
    assert all(abs(d.totals["residual_pp"]) < 1e-9 for d in a.decompositions if d.method == "MIX_RATE_DECOMPOSITION")
    assert a.interpretation_code == "MIX_DOMINANT" and a.drivers[0]["segment"] == "UPI_COLLECT"
    assert any(h.code == "MIX_SHIFT" for h in a.hypotheses)
    assert all(rc.follows_from in {h.id for h in a.hypotheses} for rc in a.recommendations)
    extra = [x for x in r.steps if x.role == "drilldown"]
    assert extra and all(x.status == "OK" for x in extra)          # drill-downs went through the gate


def test_drilldowns_respect_scope(analyst):
    s = Session("u_west", clarifications={"METRIC_DEFINITION:success_rate": "a"})
    r = analyst.ask("Why did success rate drop in the last two weeks?", "u_west", s)
    assert r.state in ANSWERED
    for x in r.steps:
        if x.status == "OK":
            assert any("WEST" in p for p in x.rls), x.purpose


def test_data_quality_checks_find_planted_issues(analyst):
    dq = analyst.dq
    ids = {r.check_id: r for r in dq.run(date(2026, 10, 1), date(2026, 10, 27), {"merchant_segment"})}
    assert ids["DQ01_DUPLICATES"].auto_mitigation == "USED_DEDUP_VIEW" and "2026-10-05" in ids["DQ01_DUPLICATES"].dates
    assert ids["DQ02_NULL_KEYS"].affects and "merchant_segment" in ids["DQ02_NULL_KEYS"].affects
    assert "DQ05_LATE_ARRIVING" in ids
    assert "DQ01_DUPLICATES" not in {r.check_id for r in dq.run(date(2026, 9, 1), date(2026, 9, 20))}


def test_reconciliation_explains_the_gap(analyst):
    r = analyst.ask("Why does finance's settled amount for October differ from the dashboard?", "u_fin",
                    Session("u_fin"))
    rec = r.analysis.reconciliation
    assert rec and rec["diff_inr"] > 0 and abs(rec["residual_inr"]) < 1
    factors = {f["factor"] for f in rec["explained"]}
    assert {"TIMEZONE_DAY_BOUNDARY", "REVERSED_EXCLUDED_IN_FINANCE"} <= factors
    assert any(x.check_id == "DQ09_SOURCE_MISMATCH" for x in r.analysis.dq)


def test_premise_contradiction_is_reported(analyst):
    s = Session("u_exec", clarifications={"METRIC_DEFINITION:success_rate": "a"})
    r = analyst.ask("Did success rate drop yesterday?", "u_exec", s)
    a = r.analysis
    assert a.premise is not None
    if not a.headline.get("significant") or a.headline["delta"] >= 0:
        assert any(h.code == "PREMISE_NOT_SUPPORTED" for h in a.hypotheses)


def test_analysis_failure_keeps_the_query_results(analyst, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad analysis")
    monkeypatch.setattr(analyst.analyzer, "analyze", boom)
    r = analyst.ask("What was GMV last week?", "u_exec")
    assert r.state == "ANSWERED_LOW_CONFIDENCE" and r.analysis is None and r.steps and r.steps[0].rows
    assert any(t["stage"] == "ANALYSIS" and t["status"] == "ERROR" for t in r.trace)
