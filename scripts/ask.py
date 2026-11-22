"""Ask the analyst a question as a given user (pipeline up to executed queries).

    python scripts/ask.py --user u_exec "Why did our payment success rate drop in the last two weeks?"
    python scripts/ask.py --user u_exec --answer METRIC_DEFINITION:success_rate=a "Why did ..."
    python scripts/ask.py --user u_exec --interactive          # multi-turn session, answers clarifications
    python scripts/ask.py --llm anthropic ...                   # force Claude (needs ANTHROPIC_API_KEY)

Without credentials the offline rule-based backend is used (shown in the header).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyst.pipeline import Analyst, PipelineResult, Session  # noqa: E402


def show(r: PipelineResult, verbose: bool) -> None:
    print(f"\n[{r.state}]")
    if r.ambiguity and r.ambiguity.items:
        print(r.message)
        for it in r.ambiguity.items:
            print(f"  ? {it.key}")
            for o in it.options:
                rec = " (recommended)" if o.id == it.recommended else ""
                print(f"     {o.id}) {o.label}" + (f" -> {o.display}" if o.display else "") + rec)
        return
    if r.narrowing:
        print(r.message)
        for o in r.narrowing["options"]:
            print(f"     {o['id']}) {o['label']}")
        print("  answer with NARROWING=<id>")
        return
    if r.answer:
        print(r.answer.to_markdown())
        if r.answer.charts:
            print("\nCharts: " + ", ".join(f"{c['chart_id']} {c['type']} ({c.get('title', '')})" for c in r.answer.charts))
        if r.answer.suggested_follow_ups:
            print("Follow-ups: " + " | ".join(r.answer.suggested_follow_ups))
        g = r.answer.grounding
        if g:
            print(f"Grounding: {g.get('checked', 0)} numbers checked, "
                  f"{'all traced to query results' if g.get('passed') else 'unverified wording replaced'}"
                  f" (composed by: {r.answer.composed_by})")
    else:
        print(r.message)
    if not verbose:
        return
    a = r.analysis
    if a:
        print(f"\n  interpretation: {a.interpretation_code} (best split: {a.best_dimension})")
        for d in a.drivers[:3]:
            print(f"  driver: {d['dimension']} = {d['segment']}  {d['contribution']:+.2f} "
                  f"({d['share_of_change']:.0%} of change; mix {d.get('mix_effect_pp') or 0:+.2f}, "
                  f"rate {d.get('rate_effect_pp') or 0:+.2f})")
    for d in r.disclosures:
        print(f"  * {d}")
    import pandas as pd
    for s in r.steps:
        print(f"\n  {s.step_id} {s.purpose}  [{s.status}, {s.row_count} rows, {s.duration_ms} ms]")
        if s.error:
            print(f"     error: {s.error}")
        if s.sql:
            print("     " + s.sql)
        if s.rows:
            with pd.option_context("display.width", 160, "display.max_columns", 12):
                print("     " + s.df().head(8).to_string(index=False).replace("\n", "\n     "))
    print("\n  trace: " + " -> ".join(f"{t['stage']}:{t['status']}({t['ms']}ms)" for t in r.trace))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="?")
    ap.add_argument("--user", default="u_exec")
    ap.add_argument("--llm", choices=["auto", "anthropic", "offline"], default=None)
    ap.add_argument("--answer", action="append", default=[], help="KEY=option, e.g. METRIC_DEFINITION:success_rate=a")
    ap.add_argument("--interactive", "-i", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    analyst = Analyst(provider=a.llm)
    session = Session(a.user)
    answers = dict(x.split("=", 1) for x in a.answer)
    print(f"LLM backend: {analyst.llm.name}  |  data as of {analyst.as_of}  |  user {a.user}")
    if not a.interactive:
        if not a.question:
            ap.error("a question is required (or use --interactive)")
        r = analyst.ask(a.question, a.user, session, answers)
        if a.json:
            print(json.dumps(r.as_dict(), indent=2, default=str))
        else:
            show(r, a.verbose)
        return 0 if r.state.startswith("ANSWERED") else 1
    question = a.question
    while True:
        if not question:
            try:
                question = input("\nyou> ").strip()
            except EOFError:
                return 0
            if not question or question in ("quit", "exit"):
                return 0
        r = analyst.ask(question, a.user, session, answers)
        answers = {}
        show(r, a.verbose)
        if r.state == "AWAITING_CLARIFICATION":
            it = r.ambiguity.items[0]
            answers[it.key] = input(f"choose [{'/'.join(o.id for o in it.options)}]> ").strip() or it.recommended
            continue                       # same question again, now with the answer
        if r.state == "AWAITING_NARROWING":
            answers["NARROWING"] = input("choose> ").strip() or "a"
            continue
        question = None


if __name__ == "__main__":
    raise SystemExit(main())
