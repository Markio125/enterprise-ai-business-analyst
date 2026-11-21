"""Print what a user may ask about: the role-filtered semantic catalog.

    python scripts/catalog.py --user u_west          # markdown (what the LLM will see)
    python scripts/catalog.py --user u_fin --json    # JSON (what GET /catalog will return)
    python scripts/catalog.py --list-users
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyst.catalog import build_catalog, render_markdown  # noqa: E402
from analyst.policies import Policy  # noqa: E402
from analyst.semantic import SemanticModel  # noqa: E402
from analyst.timeparse import detect_as_of  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", default="u_exec")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=str(ROOT / "data" / "warehouse.duckdb"))
    ap.add_argument("--list-users", action="store_true")
    a = ap.parse_args()
    policy, model = Policy.load(), SemanticModel.load()
    if a.list_users:
        for p in policy.users.values():
            print(f"{p.user_id:<12} {p.role:<24} {p.name}")
        return
    con = duckdb.connect(a.db, read_only=True) if Path(a.db).exists() else None
    as_of = detect_as_of(con)[0] if con else None
    cat = build_catalog(model, policy, policy.principal(a.user), con, as_of)
    print(json.dumps(cat, indent=2, default=str) if a.json else render_markdown(cat))


if __name__ == "__main__":
    main()
