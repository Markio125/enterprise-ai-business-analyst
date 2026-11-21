"""Validate the semantic layer and access policies against the warehouse, then
re-derive the segment-1 ground truth through the semantic compiler.

    python scripts/check_semantic.py

Exit code 1 on any problem. This proves the semantic definitions mean what the
ground truth means, so later segments can trust compiled metrics.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyst.policies import AccessDenied, Policy  # noqa: E402
from analyst.semantic import Filter, QuerySpec, SemanticModel  # noqa: E402
from analyst.timeparse import detect_as_of, resolve  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main(db: Path = ROOT / "data" / "warehouse.duckdb", truth_path: Path = ROOT / "fixtures" / "ground_truth.json") -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    con = duckdb.connect(str(db), read_only=True)
    model, policy = SemanticModel.load(), Policy.load()
    results: list[tuple[str, bool, object]] = []

    def check(name, ok, observed):
        results.append((name, bool(ok), observed))

    problems = model.validate(con)
    check("semantic model validates against warehouse", not problems, problems or "ok")
    fan = con.execute("SELECT COUNT(*) - COUNT(DISTINCT attempt_id) FROM fact_refunds").fetchone()[0]
    check("refund join does not fan out (<= 1 refund per attempt)", fan == 0, fan)

    truth = json.loads(truth_path.read_text(encoding="utf-8"))["ground_truth"] \
        if "ground_truth" in json.loads(truth_path.read_text(encoding="utf-8")) else json.loads(truth_path.read_text(encoding="utf-8"))
    as_of, ev = detect_as_of(con, model.as_of_cfg.get("completeness_ratio", 0.85))
    check("detected as-of matches ground truth", as_of.isoformat() == truth["as_of_complete_date"], ev)

    run = lambda spec, p=None: (policy.build_query(model, p, spec) if p else model.compile(spec)).run(con)  # noqa: E731
    a9 = truth["A9_denominators"]
    win = (date.fromisoformat(a9["window"][0]), date.fromisoformat(a9["window"][1]))
    for v in ("attempt_level_all", "attempt_level_excl_user_cancel", "technical", "order_level"):
        got = round(float(run(QuerySpec(metrics=[f"success_rate:{v}"], time_range=win)).iloc[0, 0]), 4)
        want = a9["technical_success_rate" if v == "technical" else v]
        check(f"A9 success_rate:{v}", abs(got - want) < 1e-4, [got, want])

    a1 = truth["A1_mix_shift"]
    p1 = resolve("last two weeks", as_of)
    p0 = p1.previous_period()
    check("A1 'last two weeks' + previous period match ground-truth windows",
          [[p0.start.isoformat(), p0.end.isoformat()], [p1.start.isoformat(), p1.end.isoformat()]]
          == [a1["periods"]["P0"], a1["periods"]["P1"]], [p0.as_dict(), p1.as_dict()])
    for label, tr, key in (("P0", p0, "sr_p0"), ("P1", p1, "sr_p1")):
        got = round(float(run(QuerySpec(metrics=["success_rate"], time_range=(tr.start, tr.end))).iloc[0, 0]), 4)
        check(f"A1 overall SR {label}", abs(got - a1["overall"][key]) < 1e-4, [got, a1["overall"][key]])
    by = run(QuerySpec(metrics=["success_rate"], dimensions=["sub_method"], time_range=(p1.start, p1.end)))
    by = dict(zip(by.sub_method, by.iloc[:, 1].round(4)))
    bad = {k: [by.get(k), v["r1"]] for k, v in a1["by_sub_method"].items() if abs(by.get(k, -1) - v["r1"]) > 1e-4}
    check("A1 per-sub-method SR (P1)", not bad, bad or "all match")

    a6 = truth["A6_source_disagreement"]
    w6 = (date.fromisoformat(a6["window"][0]), date.fromisoformat(a6["window"][1]))
    fin = policy.principal("u_fin")
    gmv = round(float(run(QuerySpec(metrics=["gmv_inr"], time_range=w6), fin).iloc[0, 0]), 2)
    check("A6 dashboard GMV (as finance user)", abs(gmv - a6["dashboard_gmv_inr"]) < 0.01, [gmv, a6["dashboard_gmv_inr"]])
    fg = round(float(run(QuerySpec(metrics=["finance_gross_inr"], time_range=w6), fin).iloc[0, 0]), 2)
    check("A6 finance ledger gross", abs(fg - a6["finance_gross_inr"]) < 0.01, [fg, a6["finance_gross_inr"]])
    st = round(float(run(QuerySpec(metrics=["settled_gmv_inr"], time_basis="transaction_date", time_range=w6),
                         fin).iloc[0, 0]), 2)
    check("A6 settled (IST transaction day)", abs(st - a6["fact_settlements_settled_ist_inr"]) < 0.01,
          [st, a6["fact_settlements_settled_ist_inr"]])

    ev = truth["eval_answers"]["EV04_top5_west_merchants_by_gmv_last_month"]
    lm = resolve("last month", as_of)
    check("EV04 'last month' window", [lm.start.isoformat(), lm.end.isoformat()] == ev["window"], lm.as_dict())
    df = run(QuerySpec(metrics=["gmv_inr"], dimensions=["merchant", "merchant_id"], time_range=(lm.start, lm.end),
                       order_by=[("gmv_inr", True)], limit=5), policy.principal("u_west"))
    got = [{"merchant_name": r.merchant, "merchant_id": r.merchant_id, "gmv_inr": round(r.gmv_inr, 2)}
           for r in df.itertuples()]
    check("EV04 top-5 WEST merchants via row-level scope", got == ev["rows"], got)

    # A10: point-in-time segment vs current segment for the week BEFORE the reclassification
    a10 = truth["A10_reclassification"]
    cut = date.fromisoformat(a10["date"])
    wk = (cut - timedelta(days=7), cut - timedelta(days=1))
    seg = run(QuerySpec(metrics=["active_merchants"], dimensions=["merchant_segment", "merchant_segment_current"],
                        time_range=wk))
    pit = int(seg[seg.merchant_segment == a10["to"]].active_merchants.sum())
    cur = int(seg[seg.merchant_segment_current == a10["to"]].active_merchants.sum())
    want = a10["active_merchants_by_segment_week_before_after"][a10["to"]]["before"]
    check("A10 point-in-time segment keeps history (week before)", abs(pit - want) <= 2, [pit, want])
    check("A10 current segment restates history", cur - pit >= a10["merchants_moved"] * 0.8, [pit, cur])

    # access-control smoke checks
    for uid, spec, should_pass in [
        ("u_ops", QuerySpec(metrics=["mdr_revenue_inr"]), False),
        ("u_ops", QuerySpec(metrics=["finance_gross_inr"]), False),
        ("u_fin", QuerySpec(metrics=["mdr_revenue_inr"]), True),
        ("u_exec", QuerySpec(metrics=["mdr_revenue_inr"]), True),
        ("u_merchant", QuerySpec(metrics=["success_rate"], dimensions=["acquirer"]), False),
        ("u_merchant", QuerySpec(metrics=["success_rate"], dimensions=["issuer_bank"]), True),
        ("u_west", QuerySpec(metrics=["gmv_inr"], filters=[Filter("region", "=", "NORTH")]), False),
        ("u_west", QuerySpec(metrics=["settled_gmv_inr"]), False),
    ]:
        try:
            run(spec, policy.principal(uid))
            ok = should_pass
        except AccessDenied:
            ok = not should_pass
        check(f"RBAC {uid}: {spec.metrics} {spec.dimensions} {[f.value for f in spec.filters]} "
              f"{'allowed' if should_pass else 'refused'}", ok, ok)

    width = max(len(n) for n, _, _ in results)
    for n, ok, obs in results:
        print(f"{'PASS' if ok else 'FAIL'}  {n:<{width}}  {'' if ok else obs}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
