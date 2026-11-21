"""End-to-end check of the SQL safety gate on the full warehouse.

    python scripts/check_safety.py

1. Ground truth through the gate: A9, A6 and EV04 via the semantic path
   (precheck -> compile -> validate -> RLS -> sealed execution) still match.
2. Attack catalog: each attempt must end in the expected status, and none may
   reach the database.
Exit code 1 on any failure.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyst.executor import QueryGateway  # noqa: E402
from analyst.policies import AccessDenied  # noqa: E402
from analyst.semantic import QuerySpec  # noqa: E402
from analyst.sqlsafety import FAIL_ACCESS, FAIL_REPAIRABLE, FAIL_UNSAFE, NEEDS_NARROWING, PASS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OCT = "date_key BETWEEN 20261001 AND 20261027"

ATTACKS = [
    ("u_ops", "stacked statement", "SELECT 1; DROP TABLE v_attempts", FAIL_UNSAFE),
    ("u_ops", "DDL", "DROP TABLE v_attempts", FAIL_UNSAFE),
    ("u_ops", "DML", "UPDATE dim_merchant SET region = 'WEST'", FAIL_UNSAFE),
    ("u_ops", "exfiltrate to file", "COPY v_attempts TO 'leak.csv'", FAIL_UNSAFE),
    ("u_ops", "read local file", "SELECT * FROM read_csv('C:/secrets.csv')", FAIL_UNSAFE),
    ("u_ops", "file as table", "SELECT * FROM 'data/warehouse.duckdb'", FAIL_UNSAFE),
    ("u_ops", "env variable", "SELECT getenv('PATH')", FAIL_UNSAFE),
    ("u_ops", "attach database", "ATTACH 'x.db' AS x", FAIL_UNSAFE),
    ("u_ops", "load extension", "LOAD httpfs", FAIL_UNSAFE),
    ("u_ops", "change settings", "SET enable_external_access = true", FAIL_UNSAFE),
    ("u_ops", "SELECT INTO", "SELECT 1 INTO t", FAIL_UNSAFE),
    ("u_ops", "comment-hidden DDL", "/* please */ DROP TABLE v_attempts", FAIL_UNSAFE),
    ("u_west", "shadow dim_merchant with a CTE",
     "WITH dim_merchant AS (SELECT 1 AS merchant_key, 'WEST' AS region) SELECT COUNT(*) FROM v_attempts WHERE " + OCT,
     FAIL_UNSAFE),
    ("u_ops", "MDR via SQL (finance-only)", "SELECT SUM(mdr_fee_paise) FROM fact_settlements WHERE settlement_date_key > 20261001", FAIL_ACCESS),
    ("u_ops", "finance ledger", "SELECT SUM(gross_amount_inr) FROM finance_daily_settlement_summary", FAIL_ACCESS),
    ("u_ops", "PII via SELECT *", "SELECT * FROM dim_merchant", FAIL_ACCESS),
    ("u_ops", "PII via CTE", "WITH s AS (SELECT legal_entity_id x FROM dim_merchant) SELECT COUNT(DISTINCT x) FROM s", FAIL_ACCESS),
    ("u_merchant", "routing via join key", "SELECT acquirer_key, COUNT(*) FROM v_attempts WHERE " + OCT + " GROUP BY 1", FAIL_ACCESS),
    ("u_exec", "system catalog", "SELECT table_name FROM information_schema.tables", FAIL_ACCESS),
    ("u_ops", "no time filter", "SELECT status FROM v_attempts", FAIL_REPAIRABLE),
    ("u_ops", "SELECT * on a fact", "SELECT * FROM v_attempts WHERE " + OCT, FAIL_REPAIRABLE),
    ("u_ops", "cartesian join", "SELECT a.status FROM v_attempts a, dim_merchant m WHERE a." + OCT, FAIL_REPAIRABLE),
    ("u_ops", "hallucinated column", "SELECT success_flag FROM v_attempts WHERE " + OCT, FAIL_REPAIRABLE),
    ("u_ops", "join explosion", "SELECT COUNT(*) FROM v_attempts a JOIN v_attempts b ON a.order_id <> b.order_id "
                                "WHERE a.date_key >= 20261001 AND b.date_key >= 20261001", NEEDS_NARROWING),
    ("u_ops", "window too long", "SELECT COUNT(*) FROM v_attempts WHERE date_key BETWEEN 20240101 AND 20261027", NEEDS_NARROWING),
]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    g = QueryGateway()
    P = g.policy.principal
    truth = json.loads((ROOT / "fixtures" / "ground_truth.json").read_text(encoding="utf-8"))
    results = []

    def check(name, ok, obs=""):
        results.append((name, bool(ok), obs))

    a9 = truth["A9_denominators"]
    win = (date.fromisoformat(a9["window"][0]), date.fromisoformat(a9["window"][1]))
    for v in ("attempt_level_all", "attempt_level_excl_user_cancel", "technical", "order_level"):
        r = g.run_spec(QuerySpec(metrics=[f"success_rate:{v}"], time_range=win), P("u_exec"))
        want = a9["technical_success_rate" if v == "technical" else v]
        got = round(float(r.rows[0][0]), 4) if r.ok else None
        check(f"gate: A9 success_rate:{v}", got == want, [r.status, got, want])
    a6 = truth["A6_source_disagreement"]
    w6 = (date.fromisoformat(a6["window"][0]), date.fromisoformat(a6["window"][1]))
    for metric, key in (("gmv_inr", "dashboard_gmv_inr"), ("finance_gross_inr", "finance_gross_inr")):
        r = g.run_spec(QuerySpec(metrics=[metric], time_range=w6), P("u_fin"))
        got = round(float(r.rows[0][0]), 2) if r.ok else None
        check(f"gate: A6 {metric} (finance user)", got == a6[key], [r.status, got, a6[key]])
    ev = truth["eval_answers"]["EV04_top5_west_merchants_by_gmv_last_month"]
    r = g.run_spec(QuerySpec(metrics=["gmv_inr"], dimensions=["merchant", "merchant_id"],
                             time_range=tuple(date.fromisoformat(d) for d in ev["window"]),
                             order_by=[("gmv_inr", True)], limit=5), P("u_west"))
    got = [{"merchant_name": a, "merchant_id": b, "gmv_inr": round(c, 2)} for a, b, c in r.rows] if r.ok else None
    check("gate: EV04 top-5 WEST merchants (RLS injected by validator)", got == ev["rows"],
          [r.status, r.validation.rls_predicates_applied if r.validation else None])
    try:
        g.run_spec(QuerySpec(metrics=["mdr_revenue_inr"]), P("u_ops"))
        check("gate: semantic precheck refuses MDR for ops", False)
    except AccessDenied:
        check("gate: semantic precheck refuses MDR for ops", True)

    for uid, label, sql, expected in ATTACKS:
        v = g.validate(sql, P(uid))
        check(f"attack [{uid}] {label} -> {expected}", v.status == expected and v.seal is None,
              [v.status, v.errors[:1]])

    # same question, three users: the WEST manager sees less, never more
    q = "SELECT COUNT(*) FROM v_attempts WHERE " + OCT
    n = {u: g.run_sql(q, P(u)).rows[0][0] for u in ("u_ops", "u_west", "u_am", "u_merchant")}
    check("RLS: scoped users see strict subsets", n["u_ops"] > n["u_west"] > 0 and n["u_ops"] > n["u_am"] > 0
          and n["u_ops"] > n["u_merchant"] > 0, n)

    width = max(len(x) for x, _, _ in results)
    for name, ok, obs in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {'' if ok else obs}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
