"""Run a metric query through the semantic layer as a given user.

    python scripts/query.py --user u_west --metric gmv_inr --by merchant --time "last month" --top 5
    python scripts/query.py --metric success_rate --by sub_method --time "last two weeks" --compare
    python scripts/query.py --user u_ops --metric mdr_revenue_inr          # -> REFUSED_ACCESS
    python scripts/query.py --metric success_rate:order_level --where "payment_method=UPI" --show-sql
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyst.policies import AccessDenied, Policy  # noqa: E402
from analyst.semantic import Filter, QuerySpec, SemanticError, SemanticModel  # noqa: E402
from analyst.timeparse import TimeParseError, detect_as_of, resolve  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _filter(s: str) -> Filter:
    for op in ("!=", ">=", "<=", "=", ">", "<"):
        if op in s:
            dim, val = s.split(op, 1)
            vals = [v.strip() for v in val.split(",")]
            if len(vals) > 1:
                return Filter(dim.strip(), "in" if op == "=" else "not in", vals)
            return Filter(dim.strip(), op, vals[0])
    raise SystemExit(f"bad --where {s!r} (use dim=value or dim=a,b)")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", default="u_exec")
    ap.add_argument("--metric", action="append", required=True, help="repeatable; name or name:variant")
    ap.add_argument("--by", action="append", default=[], help="dimension (repeatable)")
    ap.add_argument("--where", action="append", default=[], help="dim=value, dim=a,b, dim!=value ...")
    ap.add_argument("--time", help='e.g. "last month", "last two weeks", "2026-09-10"')
    ap.add_argument("--time-basis")
    ap.add_argument("--compare", action="store_true", help="also run the previous equal-length period")
    ap.add_argument("--top", type=int, help="order by the first metric descending and limit")
    ap.add_argument("--show-sql", action="store_true")
    ap.add_argument("--db", default=str(ROOT / "data" / "warehouse.duckdb"))
    a = ap.parse_args()

    con = duckdb.connect(a.db, read_only=True)
    model, policy = SemanticModel.load(), Policy.load()
    principal = policy.principal(a.user)
    as_of, _ = detect_as_of(con, model.as_of_cfg.get("completeness_ratio", 0.85))
    periods = [None]
    if a.time:
        try:
            tr = resolve(a.time, as_of)
        except TimeParseError as e:
            print(e)
            return 2
        periods = [tr] + ([tr.previous_period()] if a.compare else [])
    print(f"user {principal.name} ({principal.role}); as-of {as_of}")
    for tr in periods:
        spec = QuerySpec(metrics=a.metric, dimensions=a.by, filters=[_filter(w) for w in a.where],
                         time_range=(tr.start, tr.end) if tr else None, time_basis=a.time_basis,
                         order_by=[(a.metric[0], True)] if a.top else [], limit=a.top)
        try:
            q = policy.build_query(model, principal, spec)
        except AccessDenied as e:
            print(json.dumps(policy.refusal(model, e.auth), indent=2))
            return 3
        except SemanticError as e:
            print(f"error: {e}")
            return 2
        print(f"\n== {tr.label + ' (' + str(tr.start) + ' .. ' + str(tr.end) + ')' if tr else 'all data'}")
        if tr and tr.note:
            print(f"   note: {tr.note}")
        for d in q.disclosures:
            print(f"   * {d}")
        if a.show_sql:
            print(q.sql, "\n  params:", q.params)
        with pd.option_context("display.max_rows", 200, "display.width", 160):
            print(q.run(con).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
