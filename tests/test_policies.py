import copy
from datetime import date

import pytest

from analyst.catalog import build_catalog, render_markdown
from analyst.policies import AccessDenied, Policy, Principal
from analyst.semantic import Filter, QuerySpec, SemanticModel

M = SemanticModel.load()
P = Policy.load()
OCT = (date(2026, 10, 1), date(2026, 10, 27))


def u(uid):
    return P.principal(uid)


def test_inheritance_merges_lists_and_overrides_tables():
    west = P.roles["regional_manager_west"]
    assert west.lineage == ("regional_manager_west", "ops_analyst")
    assert "*.mdr_fee_paise" in west.deny_columns          # inherited
    assert "fact_payment_attempts_raw" not in west.tables  # overridden
    assert west.row_filters[0]["dimension"] == "region"


def test_inheritance_cycle_detected():
    raw = copy.deepcopy(P.raw)
    raw["roles"]["ops_analyst"]["inherits"] = "regional_manager_west"
    with pytest.raises(ValueError, match="cycle"):
        Policy(raw)


@pytest.mark.parametrize("uid,metric,allowed", [
    ("u_exec", "mdr_revenue_inr", True),
    ("u_fin", "mdr_revenue_inr", True),
    ("u_ops", "mdr_revenue_inr", False),
    ("u_ops", "finance_gross_inr", False),
    ("u_ops", "refund_rate", False),          # needs fact_refunds
    ("u_ops", "gmv_inr", True),
    ("u_fin", "gmv_inr", True),               # finance may reconcile against dashboard GMV
    ("u_fin", "timeout_rate", True),
    ("u_fin", "success_rate:technical", False),   # needs dim_failure_reason
    ("u_merchant", "active_merchants", False),
    ("u_merchant", "success_rate:order_level", True),
])
def test_metric_access(uid, metric, allowed):
    assert P.authorize(M, u(uid), QuerySpec(metrics=[metric])).allowed is allowed


def test_restricted_to_is_enforced_even_with_table_access():
    raw = copy.deepcopy(P.raw)
    raw["roles"]["ops_analyst"]["tables"].append("fact_settlements")
    raw["roles"]["ops_analyst"]["deny_tables"].remove("fact_settlements")
    raw["roles"]["ops_analyst"]["deny_columns"].remove("*.mdr_fee_paise")
    pol = Policy(raw)
    auth = pol.authorize(M, pol.principal("u_ops"), QuerySpec(metrics=["mdr_revenue_inr"]))
    assert not auth.allowed and any("restricted to" in d.reason for d in auth.denials)
    assert pol.authorize(M, pol.principal("u_ops"), QuerySpec(metrics=["settled_gmv_inr"])).allowed


def test_confidential_dimension_refused_for_merchant():
    auth = P.authorize(M, u("u_merchant"), QuerySpec(metrics=["success_rate"], dimensions=["acquirer"]))
    assert not auth.allowed
    r = P.refusal(M, auth)
    assert r["status"] == "REFUSED_ACCESS" and r["denied"][0]["name"] == "acquirer" and r["alternatives"]


def test_scope_is_injected_as_parameter():
    q = P.build_query(M, u("u_west"), QuerySpec(metrics=["gmv_inr"], time_range=OCT))
    assert "merchant.region = ?" in q.sql and q.params[-1] == "WEST"
    assert any("WEST" in d for d in q.disclosures)


def test_unscoped_role_gets_no_filter():
    q = P.build_query(M, u("u_ops"), QuerySpec(metrics=["gmv_inr"], time_range=OCT))
    assert "region" not in q.sql and q.params == [20261001, 20261027]


def test_account_manager_sees_only_own_merchants(con):
    df = P.build_query(M, u("u_am"), QuerySpec(metrics=["attempt_count"], dimensions=["account_manager"],
                                               time_range=OCT)).run(con)
    assert df.account_manager.tolist() == ["Divya Nair"]


def test_merchant_user_sees_only_their_merchant_even_without_dim_merchant_access(con):
    assert not P.roles["merchant_user"].table_allowed("dim_merchant")
    q = P.build_query(M, u("u_merchant"), QuerySpec(metrics=["attempt_count"], time_range=OCT))
    n = q.run(con).iloc[0, 0]
    total = M.compile(QuerySpec(metrics=["attempt_count"], time_range=OCT)).run(con).iloc[0, 0]
    assert 0 < n < total


def test_scoped_results_exclude_rows_without_merchant(con):
    a5 = (date(2026, 10, 18), date(2026, 10, 20))
    q = P.build_query(M, u("u_west"), QuerySpec(metrics=["unmapped_merchant_rows"], time_range=a5))
    assert q.run(con).iloc[0, 0] == 0
    assert M.compile(QuerySpec(metrics=["unmapped_merchant_rows"], time_range=a5)).run(con).iloc[0, 0] > 0


def test_out_of_scope_filter_refused_by_default():
    auth = P.authorize(M, u("u_west"), QuerySpec(metrics=["gmv_inr"], filters=[Filter("region", "=", "NORTH")]))
    assert not auth.allowed and auth.denials[0].type == "scope"
    assert "outside your access scope" in P.refusal(M, auth)["message"]
    assert P.authorize(M, u("u_west"), QuerySpec(metrics=["gmv_inr"], filters=[Filter("region", "=", "WEST")])).allowed


def test_out_of_scope_can_be_scoped_and_disclosed():
    raw = copy.deepcopy(P.raw)
    raw["defaults"]["out_of_scope_behaviour"] = "scope_and_disclose"
    pol = Policy(raw)
    auth = pol.authorize(M, pol.principal("u_west"), QuerySpec(metrics=["gmv_inr"],
                                                                filters=[Filter("region", "in", ["WEST", "NORTH"])]))
    assert auth.allowed and any("NORTH" in d for d in auth.disclosures)


def test_missing_user_attribute_fails_closed():
    nobody = Principal("u_x", "No Merchant", "merchant_user", {})
    auth = P.authorize(M, nobody, QuerySpec(metrics=["attempt_count"]))
    assert not auth.allowed and auth.denials[0].type == "scope"
    with pytest.raises(AccessDenied):
        P.build_query(M, nobody, QuerySpec(metrics=["attempt_count"]))


def test_unapplicable_scope_denies_entity():
    raw = copy.deepcopy(P.raw)
    raw["roles"]["hour_scoped"] = {"tables": ["*"], "row_filters": [{"dimension": "hour", "op": "=", "value": 9}]}
    pol = Policy(raw)
    p = Principal("u_h", "H", "hour_scoped")
    assert pol.authorize(M, p, QuerySpec(metrics=["attempt_count"])).allowed
    auth = pol.authorize(M, p, QuerySpec(metrics=["order_count"]))   # orders have no hour
    assert not auth.allowed and auth.denials[0].type == "entity"


def test_catalog_hides_what_role_cannot_use(con):
    cat = build_catalog(M, P, u("u_ops"), con, date(2026, 10, 27))
    names = {m["name"] for m in cat["metrics"]}
    assert "gmv_inr" in names and "mdr_revenue_inr" not in names and "settled_gmv_inr" not in names
    assert {"type": "metric", "name": "mdr_revenue_inr"} in cat["not_accessible"]
    assert "settlements" not in cat["entities"]
    assert "issuer_bank" in cat["dimension_values"] and "Meridian Bank" in cat["dimension_values"]["issuer_bank"]
    assert "revenue" not in cat["ambiguous_terms"]       # only one usable candidate left
    md = render_markdown(cat)
    assert "gmv_inr" in md and "mdr_revenue_inr" not in md.split("## Not accessible")[0]


def test_catalog_for_scoped_user_shows_scope_and_no_cross_scope_values(con):
    cat = build_catalog(M, P, u("u_west"), con)
    assert cat["scope"] == [{"dimension": "region", "op": "=", "value": "WEST"}]
    assert "region" not in cat["dimension_values"] and "merchant" not in cat["dimension_values"]
    cat = build_catalog(M, P, u("u_merchant"), con)
    assert "acquirer" not in {d["name"] for d in cat["dimensions"]}
    assert cat["scope"][0]["value"] == "MID100121"
