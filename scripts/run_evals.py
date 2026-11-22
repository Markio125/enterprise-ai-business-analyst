"""Run the evaluation suite (evals/cases.yaml) and write the report.

    python scripts/run_evals.py                      # offline backend, all cases
    python scripts/run_evals.py --llm anthropic      # through Claude (also runs the LLM judge)
    python scripts/run_evals.py --case EV01 EV08     # a subset (id or id prefix)

Writes evals/REPORT.md and evals/last_run.json, and records the run in the metadata DB (eval_runs).
Exit code 1 when the EV01-EV15 pass rate is below the 90% gate (SPEC 22).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analyst.evals import load_cases, run_suite, to_markdown  # noqa: E402
from analyst.pipeline import Analyst  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--llm", default="offline", help="offline | anthropic | auto")
    ap.add_argument("--case", nargs="*", help="case ids or prefixes (default: all)")
    ap.add_argument("--out", default=str(ROOT / "evals"), help="directory for REPORT.md and last_run.json")
    ap.add_argument("--no-store", action="store_true", help="do not record the run in the metadata DB")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    analyst = Analyst(provider=args.llm)
    store = None
    if not args.no_store:
        from analyst.store import Store
        store = Store(analyst.settings["api"]["metadata_db"])

    def show(r):
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"{mark}  {r['case_id']:<28} {r['state']:<24} {r['latency_ms']:>6} ms")
        for g, v in r["graders"].items():
            for c in v.get("checks", []):
                if not c["passed"]:
                    print(f"        {g}: {c['name']}: observed {c.get('observed')} expected {c.get('expected')}")

    report = run_suite(analyst, load_cases(ids=args.case), store=store, on_case=show)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "REPORT.md").write_text(to_markdown(report), encoding="utf-8")
    (out / "last_run.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    s = report["summary"]
    core = s["core_ev01_ev15"]
    print(f"\n{s['passed']}/{s['cases']} cases passed; EV01-EV15 {core['passed']}/{core['cases']} "
          f"(gate {'met' if s['gate']['passed'] else 'NOT met'}); p50 {s['latency_ms']['p50']} ms; "
          f"cost ${s['cost_usd']:.4f}; backend {s['llm']}")
    print(f"report: {out / 'REPORT.md'}")
    return 0 if s["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
