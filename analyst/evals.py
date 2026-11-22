"""Evaluation harness (SPEC section 18): runs evals/cases.yaml through the pipeline and grades each case.

Graders
- numeric:          executed numbers vs fixtures/ground_truth.json, within tolerance
- structural:       status, ambiguity types, caveat codes, drivers, scope, safety (what reached the executor)
- sql_equivalence:  a hand-written reference query, run directly on the warehouse, must return the same result
                    set as the step the pipeline executed (order-insensitive, numbers rounded)
- llm_judge:        a rubric judge (faithfulness, fact/hypothesis separation, clarity) that sees the evidence,
                    not just the answer. Needs a real model; with the offline backend it is reported as skipped.

A case passes when every grader that ran passed. The report gives pass rate per category and per grader,
latency, tokens and cost, and is stored in the metadata DB (eval_runs / eval_results) when a store is given.
"""
from __future__ import annotations

import json
import re
import statistics
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .pipeline import Analyst, PipelineResult, Session

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals" / "cases.yaml"
TRUTH = ROOT / "fixtures" / "ground_truth.json"
WAITING = ("AWAITING_CLARIFICATION", "AWAITING_NARROWING")
GRADERS = ("numeric", "structural", "sql_equivalence", "llm_judge")
PASS_GATE = 0.90                                  # SPEC 22: EV01-EV15 >= 90% before the demo

JUDGE_SYSTEM = """You grade answers produced by an AI business analyst for payments data.
You see the user's question, the answer (headline, facts, hypotheses, recommendations, caveats) and the
evidence: the queries that ran with their first result rows, and `computed`, the deterministic analysis
derived from those queries (decomposition into mix and rate effects, driver ranking, drill-downs, outage
windows, reconciliation). A number is supported if it appears in the rows or in `computed`, allowing for
rounding and for display as percent, pp or crore/lakh. Score 1-5 on each criterion:
- faithfulness: every number and claim in the facts is supported by the evidence rows; nothing invented.
- separation: facts are things the data shows; explanations that are not proven are labelled hypotheses;
  recommendations follow from them.
- clarity: a business reader gets the answer from the headline, with the definitions, period and scope stated.
List concrete issues (quote the sentence). Be strict: 5 means no issue at all."""
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "faithfulness": {"type": "integer"}, "separation": {"type": "integer"}, "clarity": {"type": "integer"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["faithfulness", "separation", "clarity", "issues"],
    "additionalProperties": False,
}
JUDGE_MIN = 4


# --------------------------------------------------------------------------- loading
def load_cases(path: str | Path = CASES, ids: list[str] | None = None) -> list[dict]:
    cases = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    seen = set()
    for c in cases:
        assert c["id"] not in seen, f"duplicate case id {c['id']}"
        seen.add(c["id"])
        assert ("question" in c) != ("conversation" in c), f"{c['id']}: give question or conversation"
        assert set(c.get("grading", [])) <= set(GRADERS), f"{c['id']}: unknown grader"
        assert "user" in c and "expected" in c, f"{c['id']}: user and expected are required"
    if ids:
        cases = [c for c in cases if any(c["id"] == i or c["id"].startswith(i + "_") for i in ids)]
    return cases


def resolve(v, truth: dict):
    """Replace {truth: a.b.c} with the ground-truth value, recursively."""
    if isinstance(v, dict):
        if isinstance(v.get("truth"), str):
            cur = truth
            for part in v["truth"].split("."):
                cur = cur[part]
            if set(v) == {"truth"}:
                return cur
            return {**{k: resolve(x, truth) for k, x in v.items() if k != "truth"}, "truth": cur}
        return {k: resolve(x, truth) for k, x in v.items()}
    if isinstance(v, list):
        return [resolve(x, truth) for x in v]
    return v


# --------------------------------------------------------------------------- running one case
@dataclass
class Run:
    """Everything a grader may look at for one case."""
    case: dict
    result: PipelineResult | None
    turns: list[PipelineResult] = field(default_factory=list)
    asked_types: list[str] = field(default_factory=list)
    executed_sql: list[str] = field(default_factory=list)
    latency_ms: int = 0
    error: str | None = None


class _SqlRecorder:
    """Records every SQL text that reaches the warehouse through the gate (execute and EXPLAIN).
    Only this thread's calls are recorded, so users asking questions meanwhile don't leak into a case."""

    def __init__(self, executor):
        self.executor, self.sql = executor, []
        self._exec, self._explain = executor.execute, executor.explain
        self.tid = threading.get_ident()

    def __enter__(self):
        def execute(v):
            if threading.get_ident() == self.tid:
                self.sql.append(getattr(v, "rewritten_sql", None) or str(v))
            return self._exec(v)

        def explain(sql):
            if threading.get_ident() == self.tid:
                self.sql.append(sql)
            return self._explain(sql)
        self.executor.execute, self.executor.explain = execute, explain
        return self

    def __exit__(self, *exc):
        self.executor.execute, self.executor.explain = self._exec, self._explain


def run_case(analyst: Analyst, case: dict) -> Run:
    questions = case.get("conversation") or [case["question"]]
    expected_status = case["expected"].get("status", "")
    answers_by_type = case.get("clarification_answers") or {}
    narrowing = list(case.get("narrowing") or [])
    session = Session(case["user"])
    run = Run(case, None)
    t0 = time.perf_counter()
    with _SqlRecorder(analyst.gateway.executor) as rec:
        try:
            for i, q in enumerate(questions):
                last = i == len(questions) - 1
                r = analyst.ask(q, case["user"], session)
                run.turns.append(r)
                for _ in range(6):
                    if r.state not in WAITING:
                        break
                    if r.state == "AWAITING_CLARIFICATION":
                        run.asked_types += [it.type for it in r.ambiguity.items]
                        if last and r.state == expected_status:
                            break
                        choice = {it.key: answers_by_type.get(it.type, it.recommended) for it in r.ambiguity.items}
                    else:
                        if (last and r.state == expected_status) or not narrowing:
                            break
                        choice = {"NARROWING": narrowing.pop(0)}
                    r = analyst.ask(q, case["user"], session, choice)
                    run.turns.append(r)
            run.result = r
        except Exception as e:  # noqa: BLE001 - a crash is a failed case, not a failed run
            run.error = f"{type(e).__name__}: {e}"
        run.executed_sql = list(rec.sql)
    run.latency_ms = int((time.perf_counter() - t0) * 1000)
    return run


# --------------------------------------------------------------------------- helpers over a result
def _text(r: PipelineResult) -> str:
    parts = [r.message or ""]
    if r.answer:
        parts.append(r.answer.to_markdown())
        parts += list(r.answer.suggested_follow_ups or [])
    return " ".join(parts).lower().replace("_", " ")


def _step(r: PipelineResult, ref: str | None):
    if not r.steps:
        return None
    if not ref:
        return r.steps[0]
    hits = [s for s in r.steps if s.step_id == ref or s.role == ref]
    return next((s for s in hits if s.period in (None, "P1")), hits[0] if hits else None)


def _caveat_codes(r: PipelineResult) -> set[str]:
    codes = {c["code"] for c in (r.answer.caveats if r.answer else [])}
    codes |= {d.check_id for d in (r.analysis.dq if r.analysis else [])}
    return codes


def _norm(v, nd=2):
    if isinstance(v, float):
        return round(v, nd)
    if isinstance(v, int) and not isinstance(v, bool):
        return round(float(v), nd)
    return v


class Checks:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, name: str, ok: bool, observed=None, expected=None):
        self.items.append({"name": name, "passed": bool(ok),
                           **({} if ok else {"observed": _short(observed), "expected": _short(expected)})})

    @property
    def passed(self) -> bool:
        return all(c["passed"] for c in self.items)


def _short(v, n=300):
    if v is None:
        return None
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


# --------------------------------------------------------------------------- graders
def grade_numeric(run: Run, exp: dict, analyst: Analyst) -> Checks:
    ck, r = Checks(), run.result
    an = r.analysis
    if "headline_metric" in exp:
        e = exp["headline_metric"]
        h = (an.headline if an else None) or {}
        tol = e.get("tolerance_pp", 0.1) / 100
        ok = all(h.get(k.upper()) is not None and abs(h[k.upper()] - e[k]) <= tol for k in ("p0", "p1") if k in e)
        ck.add("headline metric P0/P1 within tolerance", ok, {"p0": h.get("P0"), "p1": h.get("P1")},
               {k: e[k] for k in ("p0", "p1") if k in e})
    if "decomposition_totals" in exp:
        e = exp["decomposition_totals"]
        dec = an.decomposition(e["dimension"]) if an else None
        t = dec.totals if dec else {}
        tol = e.get("tolerance_pp", 0.01)
        ok = dec is not None and all(abs(t[k] - e[k]) <= tol for k in ("mix_effect_pp", "rate_effect_pp") if k in e) \
            and abs(t.get("residual_pp", 0)) < 1e-6
        ck.add(f"mix/rate decomposition by {e['dimension']} matches ground truth, no residual", ok, t,
               {k: e[k] for k in ("mix_effect_pp", "rate_effect_pp") if k in e})
    if "value_equals" in exp:
        s = _step(r, exp.get("value_step"))
        got = s.rows[0][0] if s and s.rows else None
        ck.add("value equals ground truth", got is not None and _norm(got) == _norm(exp["value_equals"]), got,
               exp["value_equals"])
    if "rows_equal" in exp:
        e = exp["rows_equal"]
        s = _step(r, e.get("step"))
        got = [[_norm(x) for x in row[: len(e["columns"])]] for row in (s.rows if s else [])]
        want = [[_norm(row[c]) for c in e["columns"]] for row in e["truth"]]
        ck.add("result rows equal ground truth", got == want, got, want)
    if "breakdown_equals" in exp:
        e = exp["breakdown_equals"]
        s = _step(r, e.get("step", "breakdown"))
        got = {(row[0] if row[0] is not None else e.get("null_label", "NULL")): _norm(row[1])
               for row in (s.rows if s else [])}
        want = {k: _norm(v) for k, v in e["truth"].items()}
        ck.add("breakdown (incl. unmapped bucket) equals ground truth", got == want, got, want)
    if "reconciliation_matches" in exp:
        e = exp["reconciliation_matches"]
        rec = an.reconciliation if an else None
        got = {f["factor"]: f["amount_inr"] for f in (rec["explained"] if rec else [])}
        tol = e.get("tolerance_inr", 0.01)
        ok = rec is not None and all(abs(got.get(k, float("inf")) - v) <= tol for k, v in e["factors"].items())
        ck.add("reconciliation factors match ground truth", ok, got, e["factors"])
        if "residual_abs_lt" in e:
            res = rec["residual_inr"] if rec else None
            ck.add("reconciliation residual within bound", res is not None and abs(res) < e["residual_abs_lt"], res,
                   f"< {e['residual_abs_lt']}")
    if "hour_window" in exp:
        hw = an.hour_window if an else None
        got = f"{hw['start_hour']:02d}:00-{hw['end_hour']:02d}:00" if hw else None
        ck.add("outage hour window matches ground truth", got == exp["hour_window"], got, exp["hour_window"])
    if "top_failure_code" in exp:
        fc = an.failure_codes if an else None
        got = fc and fc.get("top_code")
        ck.add("top failure code matches ground truth", got == exp["top_failure_code"], got, exp["top_failure_code"])
    if "all_rows_satisfy" in exp:
        e = exp["all_rows_satisfy"]
        s = _step(r, e.get("step"))
        cur = analyst.gateway.executor.con.cursor()
        bad = []
        for row in (s.rows if s else []):
            vals = {x[0] for x in cur.execute(e["lookup"], [row[e["column"]]]).fetchall()}
            if vals != {e["equals"]}:
                bad.append((row[e["column"]], sorted(vals)))
        ck.add(f"every returned row is in scope ({e['equals']})", s is not None and s.rows and not bad, bad, e["equals"])
    return ck


def grade_structural(run: Run, exp: dict) -> Checks:
    ck, r = Checks(), run.result
    an = r.analysis
    if "status" in exp:
        ck.add("status", r.state == exp["status"], r.state, exp["status"])
    if "status_in" in exp:
        ck.add("status is one of the allowed states", r.state in exp["status_in"], r.state, exp["status_in"])
    if "ambiguity_types" in exp:
        ck.add("asks about the expected ambiguity", set(exp["ambiguity_types"]) <= set(run.asked_types),
               run.asked_types, exp["ambiguity_types"])
    if "ambiguity_types_or_caveat" in exp:
        disclosed = " ".join(r.disclosures + [c["text"] for c in (r.answer.caveats if r.answer else [])]).lower()
        ok = all(t in run.asked_types or t.lower().replace("_", " ") in disclosed or "definition" in disclosed
                 for t in exp["ambiguity_types_or_caveat"])
        ck.add("ambiguity asked or disclosed", ok, run.asked_types, exp["ambiguity_types_or_caveat"])
    if "disclosure_mentions" in exp:
        d = " ".join(r.disclosures + [r.window.label if r.window else ""]).lower()
        ck.add("assumption disclosed", all(x in d for x in exp["disclosure_mentions"]), r.disclosures,
               exp["disclosure_mentions"])
    if "required_caveats" in exp:
        codes = _caveat_codes(r)
        missing = [c for c in exp["required_caveats"] if not any(c in x for x in codes)]
        ck.add("required caveats present", not missing, sorted(codes), exp["required_caveats"])
    if "top_driver" in exp:
        e = exp["top_driver"]
        d = an.drivers[0] if an and an.drivers else {}
        ck.add("top driver", d.get("dimension") == e["dimension"] and d.get("segment") == e["segment"],
               {k: d.get(k) for k in ("dimension", "segment")}, e)
    if "interpretation_code" in exp:
        got = an.interpretation_code if an else None
        ck.add("interpretation", got == exp["interpretation_code"], got, exp["interpretation_code"])
    if "must_mention" in exp:
        t = _text(r)
        missing = [m for m in exp["must_mention"] if m.lower() not in t]
        ck.add("mentions", not missing, missing, exp["must_mention"])
    if "must_not_claim" in exp:
        t = _text(r)
        found = [m for m in exp["must_not_claim"] if m.lower() in t]
        ck.add("makes no forbidden claim", not found, found, [])
    if "must_disclose_scope" in exp:
        scope = (r.answer.definitions_used.get("scope", "") if r.answer else "") + " ".join(r.disclosures)
        ck.add("row scope disclosed in the answer", exp["must_disclose_scope"] in scope, scope, exp["must_disclose_scope"])
    if "rls_applied" in exp:
        steps = [s for s in r.steps if s.sql]
        ok = bool(steps) and all(any(exp["rls_applied"] in p for p in s.rls) and exp["rls_applied"] in s.sql
                                 for s in steps)
        ck.add("row-level security injected into every executed query", ok,
               [s.rls for s in steps][:3], exp["rls_applied"])
    if exp.get("no_queries"):
        ck.add("no warehouse query executed", not r.steps, [s.step_id for s in r.steps], [])
    if "executor_never_received" in exp:
        bad = [s for s in run.executed_sql
               if any(re.search(rf"\b{k}\b", s, re.IGNORECASE) for k in exp["executor_never_received"])]
        ck.add("executor never received a forbidden statement", not bad, bad[:2], exp["executor_never_received"])
    if "explained_factors_include" in exp:
        rec = an.reconciliation if an else None
        got = {f["factor"] for f in (rec["explained"] if rec else [])}
        ck.add("reconciliation names the expected factors", set(exp["explained_factors_include"]) <= got, sorted(got),
               exp["explained_factors_include"])
    if exp.get("premise_corrected"):
        ok = an is not None and any(h.code == "PREMISE_NOT_SUPPORTED" for h in an.hypotheses)
        ck.add("corrects the user's premise", ok, an and [h.code for h in an.hypotheses], "PREMISE_NOT_SUPPORTED")
    if "window_ends_before" in exp:
        end = r.window.end.isoformat() if r.window else None
        ck.add("window ends before the incomplete day", end is not None and end < exp["window_ends_before"], end,
               f"< {exp['window_ends_before']}")
    if "narrowing_options_at_least" in exp:
        n = len((r.narrowing or {}).get("options", []))
        ck.add("offers narrowing options", n >= exp["narrowing_options_at_least"], n, exp["narrowing_options_at_least"])
    if exp.get("inherits_periods"):
        first = next((t for t in run.turns if t.plan), None)
        ok = first is not None and r.plan is not None and r is not first and r.plan.periods == first.plan.periods
        ck.add("follow-up inherits the periods", ok, r.plan and r.plan.periods, first and first.plan.periods)
    if "top_segment" in exp:
        vs = (an.volume_shift if an else None) or []
        top = vs[0]["segment"] if vs else (an.drivers[0]["segment"] if an and an.drivers else None)
        ck.add("top segment", top == exp["top_segment"], top, exp["top_segment"])
    if "charts_include" in exp:
        got = {c["type"] for c in (r.answer.charts if r.answer else [])}
        ck.add("charts include", set(exp["charts_include"]) <= got, sorted(got), exp["charts_include"])
    if "confidence" in exp:
        got = r.answer.confidence.get("level") if r.answer else None
        ck.add("confidence level", got == exp["confidence"], got, exp["confidence"])
    if exp.get("grounded"):
        g = r.answer.grounding if r.answer else {}
        ck.add("every number in the answer traced to evidence", bool(g.get("passed")) and not g.get("dropped"), g, True)
    return ck


def grade_sql_equivalence(run: Run, case: dict, analyst: Analyst) -> Checks:
    ck, r = Checks(), run.result
    s = _step(r, case.get("compare_step"))
    if s is None or not s.rows and not s.columns:
        ck.add("compared step executed", False, None, case.get("compare_step") or "first step")
        return ck
    cur = analyst.gateway.executor.con.cursor()
    ref = cur.execute(case["reference_sql"]).fetchall()
    width = len(ref[0]) if ref else len(s.columns)
    null = case.get("null_label")

    def key(row):
        return tuple(str(_norm(v)) if v is not None else (null or "") for v in row[:width])
    got = sorted(key(x) for x in s.rows)
    want = sorted(key(x) for x in ref)
    ck.add(f"result of {s.step_id} equals the reference query (order-insensitive)", got == want, got[:5], want[:5])
    return ck


def grade_llm_judge(run: Run, analyst: Analyst) -> tuple[Checks | None, dict]:
    """Returns (checks, usage); checks is None when no model is available (skipped)."""
    r = run.result
    if not r.answer or not r.answer.facts:
        return None, {}
    evidence = [{"id": s.step_id, "purpose": s.purpose, "columns": [c["name"] for c in s.columns],
                 "rows": s.rows[:20], "row_count": s.row_count} for s in r.steps]
    computed = _trim(r.analysis.as_dict()) if r.analysis else None
    a = r.answer
    payload = {"question": r.question, "answer": {"headline": a.headline, "facts": a.facts, "hypotheses": a.hypotheses,
                                                  "recommendations": a.recommendations, "caveats": a.caveats,
                                                  "definitions_used": a.definitions_used},
               "evidence": evidence, "computed": computed}
    try:
        rep = analyst.llm.json("judge", JUDGE_SYSTEM, json.dumps(payload, default=str), JUDGE_SCHEMA,
                               tier="reasoning", context=payload)
    except Exception:  # noqa: BLE001 - offline backend has no judge; any provider failure also skips
        return None, {}
    d = rep.data
    ck = Checks()
    for k in ("faithfulness", "separation", "clarity"):
        ck.add(f"judge: {k} >= {JUDGE_MIN}", int(d.get(k, 0)) >= JUDGE_MIN, {"score": d.get(k), "issues": d.get("issues")},
               JUDGE_MIN)
    return ck, {"model": rep.model, "input_tokens": rep.input_tokens, "output_tokens": rep.output_tokens,
                "cache_read_tokens": rep.cache_read_tokens}


def _trim(v, n=12):
    """Keep the judge's input small: round floats, cut long lists to their first n items."""
    if isinstance(v, dict):
        return {k: _trim(x, n) for k, x in v.items() if x not in (None, [], {})}
    if isinstance(v, list):
        return [_trim(x, n) for x in v[:n]]
    if isinstance(v, float):
        return round(v, 6)
    return v


# --------------------------------------------------------------------------- cost
def cost_usd(usages: list[dict], pricing: dict) -> float:
    total = 0.0
    for u in usages:
        p = pricing.get(u.get("model") or "", None)
        if not p:
            continue
        cache = u.get("cache_read_tokens", 0) or 0
        total += ((u.get("input_tokens", 0) or 0) * p["input"] + cache * p.get("cache_read", p["input"])
                  + (u.get("output_tokens", 0) or 0) * p["output"]) / 1e6
    return round(total, 6)


def _usages(run: Run) -> list[dict]:
    return [t["usage"] for r in run.turns for t in r.trace if t.get("usage")]


# --------------------------------------------------------------------------- the suite
def grade(run: Run, analyst: Analyst, truth: dict, pricing: dict) -> dict:
    case = run.case
    exp = resolve(case["expected"], truth)
    graders = case.get("grading") or ["structural"]
    out: dict[str, dict] = {}
    usages = _usages(run)
    if run.error or run.result is None:
        out = {g: {"status": "FAIL", "checks": [{"name": "run completed", "passed": False, "observed": run.error}]}
               for g in graders}
    else:
        for g in graders:
            try:
                if g == "numeric":
                    ck = grade_numeric(run, exp, analyst)
                elif g == "structural":
                    ck = grade_structural(run, exp)
                elif g == "sql_equivalence":
                    ck = grade_sql_equivalence(run, case, analyst)
                else:
                    ck, u = grade_llm_judge(run, analyst)
                    if u:
                        usages.append(u)
                    if ck is None:
                        out[g] = {"status": "SKIPPED", "reason": f"no judge model ({analyst.llm.name} backend)"}
                        continue
            except Exception as e:  # noqa: BLE001 - a grader crash fails the case, not the suite
                out[g] = {"status": "FAIL", "checks": [{"name": "grader ran", "passed": False,
                                                        "observed": f"{type(e).__name__}: {e}"}]}
                continue
            if not ck.items:
                out[g] = {"status": "SKIPPED", "reason": "no assertions for this grader"}
                continue
            out[g] = {"status": "PASS" if ck.passed else "FAIL", "checks": ck.items}
    ran = [v for v in out.values() if v["status"] != "SKIPPED"]
    r = run.result
    it = r.intent if r is not None else None
    parse = None if it is None else {      # kept so a failure can be traced to the model's reading of the question
        "question_type": it.question_type, "metrics": it.metrics, "dimensions": it.dimensions,
        "filters": it.filters, "time_phrase": it.time_phrase, "compare": it.compare, "grain": it.grain,
        "notes": it.notes}
    return {
        "case_id": case["id"], "category": case.get("category", "other"),
        "question": " → ".join(case.get("conversation") or [case["question"]]), "user": case["user"],
        "passed": bool(ran) and all(v["status"] == "PASS" for v in ran),
        "state": r.state if r else "ERROR", "headline": (r.answer.headline if r and r.answer else (r.message if r else run.error)),
        "graders": out, "parse": parse, "latency_ms": run.latency_ms, "turns": len(run.turns), "queries": len(run.executed_sql),
        "tokens_in": sum(u.get("input_tokens", 0) or 0 for u in usages),
        "tokens_out": sum(u.get("output_tokens", 0) or 0 for u in usages),
        "cost_usd": cost_usd(usages, pricing),
    }


def summarize(results: list[dict], llm: str, seconds: float, data_version: str | None) -> dict:
    def rate(rows):
        return {"cases": len(rows), "passed": sum(r["passed"] for r in rows),
                "pass_rate": round(sum(r["passed"] for r in rows) / len(rows), 3) if rows else None}
    cats = sorted({r["category"] for r in results})
    by_grader = {}
    for g in GRADERS:
        st = [r["graders"][g]["status"] for r in results if g in r["graders"]]
        by_grader[g] = {"ran": sum(s != "SKIPPED" for s in st), "passed": sum(s == "PASS" for s in st),
                        "skipped": sum(s == "SKIPPED" for s in st)}
    lat = sorted(r["latency_ms"] for r in results) or [0]
    core = [r for r in results if r["case_id"].startswith("EV")]
    core_rate = rate(core)["pass_rate"] if core else None
    return {
        **rate(results), "core_ev01_ev15": rate(core),
        "gate": {"threshold": PASS_GATE, "passed": core_rate is not None and core_rate >= PASS_GATE},
        "by_category": {c: rate([r for r in results if r["category"] == c]) for c in cats},
        "by_grader": by_grader,
        "latency_ms": {"p50": int(statistics.median(lat)), "p95": lat[min(len(lat) - 1, int(0.95 * len(lat)))],
                       "max": lat[-1]},
        "tokens": {"in": sum(r["tokens_in"] for r in results), "out": sum(r["tokens_out"] for r in results)},
        "cost_usd": round(sum(r["cost_usd"] for r in results), 4),
        "llm": llm, "data_version": data_version, "seconds": round(seconds, 1),
    }


def run_suite(analyst: Analyst, cases: list[dict] | None = None, store=None, truth: dict | None = None,
              on_case=None) -> dict:
    cases = cases if cases is not None else load_cases()
    truth = truth if truth is not None else json.loads(TRUTH.read_text(encoding="utf-8"))
    pricing = analyst.settings.get("llm", {}).get("pricing", {})
    t0 = time.perf_counter()
    results = []
    for c in cases:
        res = grade(run_case(analyst, c), analyst, truth, pricing)
        results.append(res)
        if on_case:
            on_case(res)
    dv = None
    try:
        dv = analyst.gateway.executor.data_version(["fact_payment_attempts_raw"])
    except Exception:  # noqa: BLE001
        pass
    summary = summarize(results, analyst.llm.name, time.perf_counter() - t0, dv)
    report = {"summary": summary, "results": results}
    if store is not None:
        rows = [{"case_id": r["case_id"], "passed": r["passed"], "scores": r["graders"],
                 "answer": {"state": r["state"], "headline": r["headline"]}, "latency_ms": r["latency_ms"],
                 "cost_usd": r["cost_usd"]} for r in results]
        report["run_id"] = store.record_eval(analyst.llm.name, {"provider": analyst.llm.name,
                                                                "cases": len(cases)}, summary, rows)
    return report


# --------------------------------------------------------------------------- report rendering
def to_markdown(report: dict) -> str:
    s = report["summary"]
    core = s["core_ev01_ev15"]
    L = [
        "# Evaluation report",
        "",
        f"- Backend: **{s['llm']}** · data version `{s['data_version']}` · {s['seconds']} s total",
        f"- **{s['passed']}/{s['cases']} cases passed** ({_pct(s['pass_rate'])}); core suite EV01–EV15: "
        f"{core['passed']}/{core['cases']} ({_pct(core['pass_rate'])}) — gate ≥ {int(s['gate']['threshold'] * 100)}%: "
        f"**{'met' if s['gate']['passed'] else 'NOT met'}**",
        f"- Latency per case: p50 {s['latency_ms']['p50']} ms, p95 {s['latency_ms']['p95']} ms, "
        f"max {s['latency_ms']['max']} ms",
        f"- Tokens: {s['tokens']['in']:,} in / {s['tokens']['out']:,} out · cost ${s['cost_usd']:.4f}",
        "",
        "## By category",
        "",
        "| Category | Passed | Rate |",
        "|---|---|---|",
        *[f"| {c} | {v['passed']}/{v['cases']} | {_pct(v['pass_rate'])} |" for c, v in s["by_category"].items()],
        "",
        "## By grader",
        "",
        "| Grader | Ran | Passed | Skipped |",
        "|---|---|---|---|",
        *[f"| {g} | {v['ran']} | {v['passed']} | {v['skipped']} |" for g, v in s["by_grader"].items()],
        "",
        "## Cases",
        "",
        "| Case | Category | User | Result | State | Graders | Latency |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in report["results"]:
        gs = " ".join(f"{g}:{v['status'].lower()}" for g, v in r["graders"].items())
        L.append(f"| {r['case_id']} | {r['category']} | {r['user']} | {'✅ pass' if r['passed'] else '❌ fail'} | "
                 f"{r['state']} | {gs} | {r['latency_ms']} ms |")
    fails = [r for r in report["results"] if not r["passed"]]
    if fails:
        L += ["", "## Failures", ""]
        for r in fails:
            L.append(f"### {r['case_id']}")
            L.append(f"Question: {r['question']}  ")
            L.append(f"State: {r['state']}  ")
            if r.get("parse"):
                L.append(f"Parse: `{json.dumps(r['parse'], default=str)}`  ")
            for g, v in r["graders"].items():
                for c in v.get("checks", []):
                    if not c["passed"]:
                        L.append(f"- {g} · {c['name']}: observed `{c.get('observed')}`, expected `{c.get('expected')}`")
            L.append("")
    L += ["", "## Answers", ""]
    for r in report["results"]:
        L.append(f"- **{r['case_id']}** ({r['state']}): {r['headline'] or ''}")
    return "\n".join(L) + "\n"


def _pct(x):
    return "–" if x is None else f"{x * 100:.0f}%"
