"""Run SQL through the safety gate as a given user, and show what the gate did.

    python scripts/run_sql.py --user u_west "SELECT COUNT(*) FROM v_attempts WHERE date_key >= 20261001"
    python scripts/run_sql.py --user u_ops "DROP TABLE v_attempts"
    python scripts/run_sql.py --user u_merchant --explain "SELECT acquirer_key, COUNT(*) FROM v_attempts ..."
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyst.executor import QueryGateway  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sql")
    ap.add_argument("--user", default="u_ops")
    ap.add_argument("--explain", action="store_true", help="print every validator check")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    g = QueryGateway()
    p = g.policy.principal(a.user)
    r = g.run_sql(a.sql, p)
    v = r.validation
    if a.json:
        print(json.dumps({"validation": v.as_dict(), "execution": r.as_dict()}, indent=2, default=str))
        return 0 if r.ok else 1
    print(f"user {p.name} ({p.role})  ->  {v.status}" + (f" / {r.status}" if v.ok else ""))
    if a.explain:
        for c in v.checks:
            mark = "ok " if c.passed else ("!! " if c.severity not in ("warn",) else "w  ")
            print(f"  {mark}{c.rule:<28} {c.detail}")
    for e in v.errors:
        print(f"  error: {e}")
    for w in v.warnings:
        print(f"  warning: {w}")
    for d in v.disclosures:
        print(f"  * {d}")
    if v.rls_predicates_applied:
        print("  row-level security:", *v.rls_predicates_applied, sep="\n    ")
    if v.rewritten_sql and v.rewritten_sql != a.sql:
        print("  executed SQL:\n    " + v.rewritten_sql)
    if not v.ok:
        print("\n" + v.user_message())
        return 1
    if r.error:
        print(f"  {r.status}: {r.error}")
        return 1
    print(f"\n{r.row_count} row(s) in {r.duration_ms} ms{' (truncated)' if r.truncated else ''}; data {r.data_version}")
    import pandas as pd
    with pd.option_context("display.max_rows", 50, "display.width", 160):
        print(r.df().head(50).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
