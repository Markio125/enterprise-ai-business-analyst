from datetime import date

import pytest

from analyst.semantic import Filter, QuerySpec, SemanticError, SemanticModel

M = SemanticModel.load()
OCT = (date(2026, 10, 1), date(2026, 10, 27))


def test_model_is_internally_consistent():
    assert M.validate() == []


def test_model_validates_against_warehouse(con):
    assert M.validate(con) == []


def test_point_in_time_merchant_join_has_no_is_current():
    on = M.entities["attempts"].joins["merchant"].on
    assert "is_current" not in on and "merchant_key" in on
    assert "is_current" in M.entities["attempts"].joins["merchant_current"].on


def test_joins_are_inferred_minimally():
    q = M.compile(QuerySpec(metrics=["gmv_inr"], time_range=OCT))
    assert q.joins == [] and "JOIN" not in q.sql
    q = M.compile(QuerySpec(metrics=["gmv_inr"], dimensions=["merchant_segment_current"]))
    assert q.joins == ["merchant", "merchant_current"]          # dependency pulled in, in order
    q = M.compile(QuerySpec(metrics=["success_rate:technical"]))
    assert q.joins == ["failure_reason"]


def test_values_are_parameters_not_inlined():
    q = M.compile(QuerySpec(metrics=["attempt_count"], filters=[Filter("issuer_bank", "=", "x' OR 1=1 --")],
                            time_range=OCT))
    assert "OR 1=1" not in q.sql and q.params == [20261001, 20261027, "x' OR 1=1 --"]


def test_default_variant_is_disclosed():
    q = M.compile(QuerySpec(metrics=["success_rate"]))
    assert q.metrics[0].variant.name == "attempt_level_excl_user_cancel"
    assert any("attempt_level_excl_user_cancel" in d for d in q.disclosures)
    assert M.compile(QuerySpec(metrics=["gmv_inr"])).disclosures == []


def test_ambiguous_time_basis_is_disclosed():
    q = M.compile(QuerySpec(metrics=["settled_gmv_inr"], time_range=OCT))
    assert "settlement_date_key" in q.sql and any("settlement_date" in d for d in q.disclosures)
    q = M.compile(QuerySpec(metrics=["settled_gmv_inr"], time_range=OCT, time_basis="transaction_date"))
    assert "attempt.date_key BETWEEN" in q.sql and q.joins == ["attempt"]


def test_complement_metric(con):
    sr = M.compile(QuerySpec(metrics=["success_rate:attempt_level_all"], time_range=OCT)).run(con).iloc[0, 0]
    fr = M.compile(QuerySpec(metrics=["failure_rate:attempt_level_all"], time_range=OCT)).run(con).iloc[0, 0]
    assert sr + fr == pytest.approx(1.0)


def test_metrics_and_order_by(con):
    df = M.compile(QuerySpec(metrics=["success_rate", "attempt_count"], dimensions=["payment_method"],
                             time_range=OCT, order_by=[("success_rate", True)], limit=3)).run(con)
    assert len(df) == 3 and df.iloc[:, 1].is_monotonic_decreasing


def test_point_in_time_vs_current_segment_differs(con):
    # Before the A10 reclassification, point-in-time and current segment disagree for ~30 merchants
    df = M.compile(QuerySpec(metrics=["active_merchants"], dimensions=["merchant_segment", "merchant_segment_current"],
                             time_range=(date(2026, 8, 25), date(2026, 8, 31)))).run(con)
    moved = df[(df.merchant_segment == "SMB") & (df.merchant_segment_current == "MID_MARKET")]
    assert len(moved) == 1 and moved.active_merchants.iloc[0] > 0


@pytest.mark.parametrize("spec,match", [
    (QuerySpec(metrics=["nope"]), "unknown metric"),
    (QuerySpec(metrics=["success_rate:nope"]), "no variant"),
    (QuerySpec(metrics=["gmv_inr", "settled_gmv_inr"]), "different entities"),
    (QuerySpec(metrics=["finance_gross_inr"], dimensions=["acquirer"]), "not available"),
    (QuerySpec(metrics=["gmv_inr"], dimensions=["nope"]), "unknown dimension"),
    (QuerySpec(metrics=["gmv_inr"], time_basis="settlement_date"), "no time basis"),
    (QuerySpec(metrics=["gmv_inr"], time_range=(date(2026, 10, 2), date(2026, 10, 1))), "after end"),
    (QuerySpec(metrics=["gmv_inr"], order_by=[("attempt_count", True)]), "cannot order"),
    (QuerySpec(metrics=[]), "at least one"),
])
def test_bad_requests_raise(spec, match):
    with pytest.raises(SemanticError, match=match):
        M.compile(spec)


def test_lookup_synonyms_and_ambiguity():
    assert M.lookup("approval rate") == [{"kind": "metric", "name": "success_rate"}]
    assert M.lookup("bank") == [{"kind": "dimension", "name": "issuer_bank"}]
    assert M.lookup("checkout conversion") == [{"kind": "metric", "name": "success_rate:order_level"}]
    rev = M.lookup("revenue")
    assert rev[0]["kind"] == "ambiguous" and "mdr_revenue_inr" in rev[0]["candidates"]
    assert M.lookup("weather") == []


def test_validate_catches_bad_definitions():
    raw = M.raw | {"dimensions": M.raw["dimensions"] | {"bad": {"expr": "merchant.no_such_col"},
                                                          "bad2": {"expr": "status"}}}
    bad = SemanticModel(raw)
    probs = "\n".join(bad.validate())
    assert "unqualified column 'status'" in probs
    raw["metrics"] = M.raw["metrics"] | {"m": {"entity": "attempts", "sql": "{missing_measure}"}}
    assert any("missing_measure" in p for p in SemanticModel(raw).validate())
