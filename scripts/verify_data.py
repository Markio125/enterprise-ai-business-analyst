"""Verify planted anomalies and write fixtures/ground_truth.json."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from analyst.data_checks import run_checks  # noqa: E402
from analyst.datagen import DEFAULT_CONFIG, DEFAULT_OUT, config_sha, load_config  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(DEFAULT_OUT))
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--truth-out", default=str(ROOT / "fixtures" / "ground_truth.json"))
    p.add_argument("--no-write", action="store_true")
    args = p.parse_args()

    cfg = load_config(args.config)
    con = duckdb.connect(args.db, read_only=True)
    rep = run_checks(con, cfg)

    width = max(len(c.name) for c in rep.checks)
    for c in rep.checks:
        mark = "PASS" if c.passed else "FAIL"
        print(f"[{mark}] {c.anomaly:<5} {c.name:<{width}}  observed={c.observed}  expected {c.expectation}")
    passed = sum(c.passed for c in rep.checks)
    print(f"\n{passed}/{len(rep.checks)} checks passed")

    if not args.no_write:
        out = {"generated_with": {"seed": cfg["seed"], "config_sha": config_sha(cfg)}} | rep.truth
        Path(args.truth_out).write_text(json.dumps(out, indent=2, default=str))
        print(f"ground truth written to {args.truth_out}")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
