"""Play the demo script (docs/DEMO.md, SPEC section 22) in the terminal, no browser needed.

    python scripts/demo.py               # every scene
    python scripts/demo.py --scene 4 5   # some scenes
    python scripts/demo.py --pause       # wait for Enter between scenes (for recording)

Each scene asks its question(s) as the scene's user, shows a clarification the way the UI does and
answers it, then prints the answer's headline, facts, hypotheses, recommendations, caveats and the
evidence that matters for that scene (for example the row-level security predicate).
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analyst.pipeline import Analyst, PipelineResult, Session  # noqa: E402

SCENES = [
    ("Mix shift, not a performance drop", "u_exec",
     ["Why did our payment success rate drop in the last two weeks?"], {"METRIC_DEFINITION": "a"}, "charts"),
    ("Follow-up drill-down in the same conversation", "u_exec",
     None, {}, "followup"),
    ("Issuer outage on one evening", "u_exec",
     ["What happened to card success on September 10?"], {"METRIC_DEFINITION": "a"}, "charts"),
    ("Access control: restricted metric", "u_ops",
     ["Show me MDR revenue by merchant last month"], {}, "refusal"),
    ("Access control: row-level security", "u_west",
     ["Top 5 merchants by GMV last month"], {}, "rls"),
    ("Unsafe request", "u_exec",
     ["Delete duplicate transactions from October 5"], {}, "plain"),
    ("Two sources disagree", "u_fin",
     ["Why does finance's settled amount for October differ from the dashboard?"], {}, "recon"),
    ("Data quality: late-arriving data", "u_exec",
     ["Did success rate drop yesterday?"], {"METRIC_DEFINITION": "a"}, "plain"),
]
W = 100


def say(text="", indent=0):
    pad = " " * indent
    for para in str(text).split("\n"):
        print(textwrap.fill(para, W, initial_indent=pad, subsequent_indent=pad) if para else "")


def show_answer(r: PipelineResult, kind: str):
    a = r.answer
    print(f"  state: {r.state}")
    if a is None:
        say(r.message, 2)
        return
    say(f"► {a.headline}", 2)
    if a.confidence.get("level") and a.facts:
        print(f"  confidence: {a.confidence['level']} ({a.confidence.get('score')}) · "
              f"{a.grounding.get('checked', 0)} numbers checked against query results")
    for label, items, key in (("Facts", a.facts, "text"), ("Hypotheses", a.hypotheses, "text"),
                              ("Recommendations", a.recommendations, "text")):
        if items:
            print(f"  {label}:")
            for it in items[:5]:
                extra = f" [{it.get('confidence')}]" if label == "Hypotheses" else ""
                extra += f" (impact {it['impact_estimate']})" if it.get("impact_estimate") else ""
                say(f"- {it['id']}{extra}: {it[key]}", 4)
    if a.caveats:
        print("  Caveats:")
        for c in a.caveats[:4]:
            say(f"- {c['text']}", 4)
    if kind == "charts" and a.charts:
        print("  Charts: " + ", ".join(f"{c['type']} '{c.get('title')}'" for c in a.charts))
    if kind == "refusal" and r.refusal:
        print("  Why: " + "; ".join(sorted({d["reason"] for d in r.refusal["denied"]})))
    if kind == "rls" and r.steps:
        s = r.steps[0]
        print("  Access rules applied by the gate: " + "; ".join(s.rls))
        print("  SQL that ran:")
        say(s.sql, 4)
    if kind == "recon" and r.analysis and r.analysis.reconciliation:
        rc = r.analysis.reconciliation
        print("  Reconciliation:")
        for f in rc["explained"]:
            print(f"    {f['factor']:<32} ₹{f['amount_inr']:>16,.2f}")
        print(f"    {'RESIDUAL':<32} ₹{rc['residual_inr']:>16,.2f}")
    if a.suggested_follow_ups:
        print("  Suggested follow-ups: " + " | ".join(a.suggested_follow_ups[:3]))


def ask(an: Analyst, session: Session, user: str, q: str, answers: dict, kind: str) -> PipelineResult:
    print(f"\n  [{user}] {q}")
    known = dict(getattr(session, "clarifications", {}) or {})
    r = an.ask(q, user, session)
    if known and r.state != "AWAITING_CLARIFICATION" and any(k.startswith("METRIC_DEFINITION") for k in known):
        print("  (no clarification: this conversation already chose the success-rate definition)")
    while r.state == "AWAITING_CLARIFICATION":
        print("  Clarification asked:")
        for it in r.ambiguity.items:
            say(it.question_to_user.split(":")[0] + ":", 4)
            for o in it.options:
                mark = "*" if o.id == answers.get(it.type, it.recommended) else " "
                print(f"     {mark} ({o.id}) {o.label} = {o.display}")
        choice = {it.key: answers.get(it.type, it.recommended) for it in r.ambiguity.items}
        print(f"  → choosing {', '.join(choice.values())}")
        r = an.ask(q, user, session, choice)
    show_answer(r, kind)
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scene", nargs="*", type=int)
    ap.add_argument("--pause", action="store_true")
    ap.add_argument("--llm", default="offline")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    an = Analyst(provider=args.llm)
    print(f"Enterprise AI Business Analyst · data complete to {an.as_of} · language model: {an.llm.name}")
    sessions: dict[str, Session] = {}
    wanted = set(args.scene or range(1, len(SCENES) + 2))
    for i, (title, user, questions, answers, kind) in enumerate(SCENES, 1):
        if i not in wanted:
            continue
        print("\n" + "=" * W + f"\nScene {i}: {title}\n" + "=" * W)
        session = sessions.setdefault(user, Session(user))
        if kind == "followup":
            if 1 not in wanted:          # the follow-up needs scene 1's context in the same session
                ask(an, session, user, SCENES[0][2][0], SCENES[0][3], "plain")
            ask(an, session, user, "Drill into UPI Collect by merchant", {}, "plain")
            continue
        for q in questions:
            ask(an, session, user, q, answers, kind)
        if args.pause:
            input("\n  (Enter for the next scene) ")
    if len(SCENES) + 1 in wanted:
        print("\n" + "=" * W + f"\nScene {len(SCENES) + 1}: Evaluation report\n" + "=" * W)
        last = ROOT / "evals" / "last_run.json"
        if last.exists():
            s = json.loads(last.read_text(encoding="utf-8"))["summary"]
            core = s["core_ev01_ev15"]
            print(f"  {s['passed']}/{s['cases']} cases passed; EV01-EV15 {core['passed']}/{core['cases']}; "
                  f"gate >= 90% {'met' if s['gate']['passed'] else 'NOT met'}; backend {s['llm']}")
            for c, v in s["by_category"].items():
                print(f"    {c:<14} {v['passed']}/{v['cases']}")
            print("  Full report: evals/REPORT.md (re-run with: python scripts/run_evals.py)")
        elif (ROOT / "evals" / "REPORT.md").exists():     # committed report, from the last published run
            for line in (ROOT / "evals" / "REPORT.md").read_text(encoding="utf-8").splitlines()[2:6]:
                print("  " + line.lstrip("- ").replace("**", ""))
            print("  Full report: evals/REPORT.md (re-run with: python scripts/run_evals.py)")
        else:
            print("  Run python scripts/run_evals.py first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
