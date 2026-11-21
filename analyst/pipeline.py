"""The question pipeline (SPEC section 7), up to executed query results.

RECEIVED -> PARSING -> (REJECTED_UNSAFE | ANSWERED_DIRECT)
         -> ACCESS_PRECHECK -> (REFUSED_ACCESS)
         -> AMBIGUITY_CHECK -> (AWAITING_CLARIFICATION)
         -> PLANNING -> (AWAITING_NARROWING)
         -> SQL_GENERATION / VALIDATION / EXECUTION (repair loop) -> EXECUTED

EXECUTED is the hand-off point to the analysis engine (segment 5) and the answer
composer (segment 6), which add ANALYSIS, COMPOSITION and the ANSWERED* states.
Every stage appends to `trace` (stage, status, latency, tokens) for the
"show steps" panel and the run_steps table (segment 7).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

from .ambiguity import AmbiguityDetector, AmbiguityReport, apply_choices, resolve_merchants
from .catalog import build_catalog, render_markdown
from .executor import QueryGateway
from .intent import INTENT_SCHEMA, PARSE_RULES, Intent, normalize, offline_parse
from .llm import LLM, LLMError, make_llm
from .planner import Plan, Planner, default_window
from .policies import AccessDenied, Authorization, Principal
from .semantic import Filter, QuerySpec
from .sqlgen import SqlGenerator
from .timeparse import TimeParseError, TimeRange, detect_as_of, resolve

TERMINAL = {"EXECUTED", "ANSWERED_DIRECT", "REFUSED_ACCESS", "REJECTED_UNSAFE", "FAILED"}
WAITING = {"AWAITING_CLARIFICATION", "AWAITING_NARROWING"}


@dataclass
class Session:
    """Conversation state: earlier intents (for follow-ups) and the user's clarification answers."""
    user_id: str
    clarifications: dict[str, str] = field(default_factory=dict)
    history: list[Intent] = field(default_factory=list)
    pending_question: str | None = None
    clarification_turns: int = 0
    pending_narrowing: dict | None = None          # {"question", "options"} from the last AWAITING_NARROWING
    narrowing_changes: list[dict] = field(default_factory=list)

    def answer(self, key: str, option_id: str) -> None:
        self.clarifications[key] = option_id


@dataclass
class StepResult:
    step_id: str
    purpose: str
    status: str
    period: str | None = None
    dimension: str | None = None
    sql: str | None = None
    columns: list[dict] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    duration_ms: int = 0
    data_version: str | None = None
    disclosures: list[str] = field(default_factory=list)
    rls: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    error: str | None = None

    def df(self):
        import pandas as pd
        return pd.DataFrame(self.rows, columns=[c["name"] for c in self.columns])

    def as_dict(self, max_rows: int | None = None) -> dict:
        d = dict(self.__dict__)
        if max_rows is not None:
            d["rows"] = self.rows[:max_rows]
        return d


@dataclass
class PipelineResult:
    state: str
    question: str
    user: dict
    message: str = ""
    intent: Intent | None = None
    window: TimeRange | None = None
    baseline: TimeRange | None = None
    metrics: list[str] = field(default_factory=list)
    ambiguity: AmbiguityReport | None = None
    plan: Plan | None = None
    steps: list[StepResult] = field(default_factory=list)
    refusal: dict | None = None
    narrowing: dict | None = None
    disclosures: list[str] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    llm: str = ""

    def step(self, sid: str) -> StepResult | None:
        return next((s for s in self.steps if s.step_id == sid), None)

    def as_dict(self, max_rows: int = 50) -> dict:
        return {
            "state": self.state, "question": self.question, "user": self.user, "message": self.message,
            "llm": self.llm,
            "intent": self.intent.as_dict() if self.intent else None,
            "window": self.window.as_dict() if self.window else None,
            "baseline": self.baseline.as_dict() if self.baseline else None,
            "metrics": self.metrics,
            "ambiguity": self.ambiguity.as_dict() if self.ambiguity else None,
            "plan": self.plan.as_dict() if self.plan else None,
            "steps": [s.as_dict(max_rows) for s in self.steps],
            "refusal": self.refusal, "narrowing": self.narrowing,
            "disclosures": self.disclosures, "trace": self.trace,
        }


class Analyst:
    """Wires the stages together. One instance serves many users and sessions."""

    def __init__(self, gateway: QueryGateway | None = None, llm: LLM | None = None, provider: str | None = None):
        self.gateway = gateway or QueryGateway()
        self.settings = self.gateway.settings
        self.model, self.policy = self.gateway.model, self.gateway.policy
        self.llm = llm or make_llm(self.settings, OFFLINE_HANDLERS, provider)
        self.sqlgen = SqlGenerator(self.llm, self.gateway, self.settings)
        self.planner = Planner(self.model, self.settings)
        self.detector = AmbiguityDetector(self.model, self.gateway, self.settings)
        con = self.gateway.executor.con
        cfg = self.model.as_of_cfg
        if cfg.get("as_of_override"):
            self.as_of, self.as_of_evidence = date.fromisoformat(str(cfg["as_of_override"])), {"rule": "override"}
        else:
            self.as_of, self.as_of_evidence = detect_as_of(con, float(cfg.get("completeness_ratio", 0.85)))
        lo = con.execute("SELECT MIN(date_key) FROM v_attempts").fetchone()[0]
        self.data_start = date(lo // 10000, lo // 100 % 100, lo % 100)
        self._catalogs: dict[str, dict] = {}

    # ------------------------------------------------------------------ helpers
    def catalog(self, p: Principal) -> dict:
        if p.user_id not in self._catalogs:
            con = self.gateway.executor.con.cursor()   # same sandboxed, read-only connection
            try:
                self._catalogs[p.user_id] = build_catalog(self.model, self.policy, p, con, self.as_of)
            finally:
                con.close()
        return self._catalogs[p.user_id]

    def _parse(self, question: str, p: Principal, session: Session, trace) -> tuple[Intent, list[str]]:
        cat = self.catalog(p)
        prev = session.history[-1] if session.history else None
        system = PARSE_RULES + "\n" + render_markdown(cat)
        user = f"Question: {question}"
        if prev:
            user = (f"Previous question: {prev.question}\nPrevious intent: metrics={prev.metrics}, "
                    f"filters={prev.filters}, time={prev.time_phrase!r}\n\n" + user)
        ctx = {"question": question, "model": self.model, "catalog": cat,
               "previous": prev.as_dict() if prev else None}
        retries = int(self.settings["llm"].get("max_json_retries", 2))
        problems: list[str] = []
        for attempt in range(retries + 1):
            t0 = time.perf_counter()
            msg = user + ("\n\nYour previous answer had problems:\n- " + "\n- ".join(problems) if problems else "")
            try:
                reply = self.llm.json("parse", system, msg, INTENT_SCHEMA, tier="fast", context=ctx)
            except LLMError as e:
                trace("PARSING", "LLM_ERROR", {"attempt": attempt + 1, "error": str(e)}, t0)
                if attempt == retries:
                    raise
                problems = [str(e)]
                continue
            intent, problems = normalize(reply.data, question, self.model, cat, prev)
            trace("PARSING", "OK" if not problems else "RETRY", {"attempt": attempt + 1, "problems": problems,
                                                                  "question_type": intent.question_type},
                  t0, reply.usage())
            if not problems:
                break
        return intent, problems

    def _time(self, intent: Intent) -> tuple[TimeRange | None, TimeRange | None, list[str]]:
        notes = []
        if intent.time_phrase:
            try:
                w = resolve(intent.time_phrase, self.as_of)
            except TimeParseError:
                w = default_window(self.as_of, int(self.settings["defaults"].get("default_time_window_days", 30)))
                notes.append(f"Couldn't read the period {intent.time_phrase!r}; used {w.label} instead.")
        else:
            w = default_window(self.as_of, int(self.settings["defaults"].get("default_time_window_days", 30)))
        w = w.clipped(self.data_start, self.as_of)
        if w.note and not w.ambiguous:
            notes.append(w.note)
        if intent.time_phrase and w.end == self.as_of and self.as_of_evidence.get("incomplete_days"):
            days = ", ".join(str(d["date_key"]) for d in self.as_of_evidence["incomplete_days"])
            notes.append(f"Data is complete up to {self.as_of}; later days ({days}) are still arriving and excluded.")
        base = None
        if intent.compare in ("previous_period", "explicit") and intent.question_type != "TREND":
            if intent.compare == "explicit" and intent.comparison_phrase:
                try:
                    base = resolve(intent.comparison_phrase, self.as_of)
                except TimeParseError:
                    base = None
            base = base or w.previous_period()
            if base.start < self.data_start:
                notes.append("The comparison period starts before the data; it was shortened.")
                base = base.clipped(self.data_start, self.as_of)
            notes.append(f"Compared {w.start}..{w.end} with the previous period {base.start}..{base.end}.")
        return w, base, notes

    def _precheck(self, refs: list[str], intent: Intent, filters: list[Filter], p: Principal) -> Authorization | None:
        """Every metric must be allowed with the requested breakdowns and filters."""
        for ref in refs:
            ent = self.model.resolve_metric(ref).entity
            dims = [d for d in intent.dimensions if d in self.model.dimensions
                    and self.model.dimension_available(d, ent)]
            fl = [f for f in filters if self.model.dimension_available(f.dimension, ent)]
            auth = self.policy.authorize(self.model, p, QuerySpec(metrics=[ref], dimensions=dims, filters=fl))
            if not auth.allowed:
                return auth
        return None

    # ------------------------------------------------------------------ main entry
    def ask(self, question: str, user_id: str, session: Session | None = None,
            answers: dict[str, str] | None = None) -> PipelineResult:
        session = session or Session(user_id)
        p = self.policy.principal(user_id)
        role = self.policy.role_of(p)
        res = PipelineResult("RECEIVED", question, {"id": p.user_id, "name": p.name, "role": role.name},
                             llm=self.llm.name)
        started = time.perf_counter()

        def trace(stage, status, detail=None, t0=None, usage=None):
            res.trace.append({"stage": stage, "status": status,
                              "ms": int((time.perf_counter() - (t0 or started)) * 1000),
                              **({"detail": detail} if detail else {}), **({"usage": usage} if usage else {})})

        for k, v in (answers or {}).items():
            session.answer(k, v)
        if session.pending_question and question != session.pending_question:
            session.clarification_turns = 0
        pn = session.pending_narrowing
        if pn and pn["question"] == question and "NARROWING" in session.clarifications:
            choice = session.clarifications.pop("NARROWING")
            opt = next((o for o in pn["options"] if o["id"] == choice), None)
            if opt:
                session.narrowing_changes.append(opt["change"])
        elif pn is None or pn["question"] != question:
            session.narrowing_changes = []
        session.pending_narrowing = None
        session.pending_question = None

        # PARSING
        try:
            intent, problems = self._parse(question, p, session, trace)
        except LLMError as e:
            return self._end(res, "FAILED", f"I couldn't understand the question right now ({e}). "
                                            "You can browse the catalog of metrics instead.", trace)
        res.intent = intent
        top_n = self._apply_narrowing(intent, session.narrowing_changes, res)
        if problems:
            res.disclosures += [f"Ignored: {x}" for x in problems]

        if intent.requests_data_change:
            return self._end(res, "REJECTED_UNSAFE",
                             "I can only read data: I can't delete, change or add records. I can show you the "
                             "data instead (for example, how many duplicate records there are and how they are "
                             "handled in reporting).", trace)
        if intent.question_type == "OFF_TOPIC":
            return self._end(res, "ANSWERED_DIRECT",
                             "I answer questions about payments data: success rates, GMV, settlements, refunds and "
                             "the drivers behind changes. Forecasts and opinions are outside what I can back with "
                             "data.", trace)
        if intent.unknown_terms and not intent.metrics and not intent.ambiguous_terms:
            cat = self.catalog(p)
            names = ", ".join(m["name"] for m in cat["metrics"][:12])
            return self._end(res, "ANSWERED_DIRECT",
                             f"There is no data for {', '.join(intent.unknown_terms)} in the datasets you can "
                             f"access. Available metrics include: {names}.", trace)
        if intent.question_type == "DEFINITION":
            return self._end(res, "ANSWERED_DIRECT", self._definitions(intent, p), trace)
        if not intent.metrics and not intent.ambiguous_terms:
            return self._end(res, "ANSWERED_DIRECT",
                             "Which metric do you mean? For example success rate, GMV, transactions or refunds.",
                             trace)

        # TIME
        try:
            window, baseline, tnotes = self._time(intent)
        except TimeParseError as e:
            return self._end(res, "ANSWERED_DIRECT", f"{e}. Data is available from {self.data_start} to "
                                                     f"{self.as_of}.", trace)
        res.window, res.baseline = window, baseline
        res.disclosures += tnotes

        # ACCESS_PRECHECK (default variants for the check; every variant is checked again later)
        t0 = time.perf_counter()
        filters = [Filter(f["dimension"], f["op"], f["values"][0] if len(f["values"]) == 1 and f["op"] in ("=", "!=")
                          else f["values"]) for f in intent.filters if f["dimension"] not in ("merchant",)]
        merchant_names = [v for f in intent.filters if f["dimension"] == "merchant" for v in f["values"]]
        auth = self._precheck(intent.metrics, intent, filters, p)
        if auth is None and merchant_names:
            auth = self._precheck(intent.metrics or ["gmv_inr"], intent,
                                  filters + [Filter("merchant", "=", merchant_names[0])], p)
        if auth is not None:
            res.refusal = self.policy.refusal(self.model, auth)
            trace("ACCESS_PRECHECK", "REFUSED", {"denied": [d.__dict__ for d in auth.denials]}, t0)
            return self._end(res, "REFUSED_ACCESS", res.refusal["message"], trace, t0)
        entity_items = []
        if merchant_names:
            found, missing, entity_items = resolve_merchants(self.gateway, p, merchant_names)
            if missing:
                trace("ACCESS_PRECHECK", "NOT_FOUND", {"missing": missing}, t0)
                where = "within your access scope" if role.row_filters else "in the data"
                return self._end(res, "ANSWERED_DIRECT",
                                 f"I couldn't find a merchant matching {', '.join(map(repr, missing))} {where}.",
                                 trace, t0)
            for i, item in enumerate(entity_items):
                ch = session.clarifications.get(item.key)
                if ch and item.option(ch):
                    found.append(item.option(ch).value)
            entity_items = [it for it in entity_items if it.key not in session.clarifications]
            if found:
                filters.append(Filter("merchant", "in" if len(found) > 1 else "=",
                                      found if len(found) > 1 else found[0]))
        if top_n:
            found = self._top_entities(top_n, intent, filters, p)
            if found:
                filters.append(Filter(top_n["dimension"], "in", found))
                res.disclosures.append(f"Limited to the top {len(found)} {top_n['dimension']}s by attempts.")
        trace("ACCESS_PRECHECK", "OK", None, t0)

        # AMBIGUITY_CHECK
        t0 = time.perf_counter()
        max_turns = int(self.settings.get("ambiguity", {}).get("max_clarification_turns", 2))
        force = session.clarification_turns >= max_turns
        rep = self.detector.detect(intent, self.catalog(p), p, window, filters, session.clarifications, force)
        rep.items = entity_items + rep.items
        res.ambiguity = rep
        trace("AMBIGUITY_CHECK", rep.action, {"items": [i.key for i in rep.items], "assumed": rep.assumptions}, t0)
        if rep.items:
            session.pending_question = question
            session.clarification_turns += 1
            first = rep.items[0]
            return self._end(res, "AWAITING_CLARIFICATION", first.question_to_user, trace, t0)
        if force:
            res.disclosures.append("Proceeded with default interpretations after the clarification limit.")
        refs, window = apply_choices(intent, rep, window)
        if window.ambiguous:   # vague time resolved by default without asking
            res.disclosures.append(window.note)
        if baseline and res.window != window:
            baseline = window.previous_period()
        res.window, res.baseline = window, baseline
        res.metrics = refs
        res.disclosures += rep.assumptions
        if not refs:
            return self._end(res, "ANSWERED_DIRECT", "Which metric do you mean?", trace)
        auth = self._precheck(refs, intent, filters, p)   # chosen variants may differ in access
        if auth is not None:
            res.refusal = self.policy.refusal(self.model, auth)
            return self._end(res, "REFUSED_ACCESS", res.refusal["message"], trace)

        # PLANNING
        t0 = time.perf_counter()
        cat = self.catalog(p)
        plan = self.planner.build(intent, refs, window, baseline, filters, cat)
        res.plan = plan
        res.disclosures += plan.notes
        mc = self._merchant_count(p) if any(s.spec and {"merchant", "merchant_id"} & set(s.spec.dimensions)
                                            for s in plan.query_steps()) else 0
        narrow = self.planner.check_size(plan, cat, mc)
        trace("PLANNING", "NARROW" if narrow else "OK",
              {"steps": len(plan.steps), "queries": len(plan.query_steps())}, t0)
        if narrow:
            res.narrowing = narrow.__dict__
            session.pending_narrowing = {"question": question, "options": narrow.options}
            return self._end(res, "AWAITING_NARROWING", narrow.message, trace, t0)

        # SQL_GENERATION / VALIDATION / EXECUTION
        for step in plan.query_steps():
            t0 = time.perf_counter()
            sr = self._run_step(step, p, cat, window, trace)
            res.steps.append(sr)
            trace("EXECUTION", sr.status, {"step": step.id, "rows": sr.row_count}, t0)
            if sr.status in ("REJECTED_UNSAFE", "REFUSED_ACCESS", "AWAITING_NARROWING"):
                if sr.status == "REFUSED_ACCESS":
                    res.refusal = {"status": "REFUSED_ACCESS", "message": sr.error, "denied": [],
                                   "alternatives": [], "role": role.name}
                return self._end(res, sr.status, sr.error or "", trace)
        failed = [s for s in res.steps if s.status not in ("OK", "TRUNCATED")]
        if failed and len(failed) == len(res.steps):
            return self._end(res, "FAILED", failed[0].error or "All queries failed.", trace)
        seen = set(res.disclosures)
        for s in res.steps:
            for d in s.disclosures:
                if d not in seen:
                    res.disclosures.append(d)
                    seen.add(d)
        session.history.append(intent)
        msg = f"Ran {len(res.steps)} quer{'y' if len(res.steps) == 1 else 'ies'} for {window.label}."
        if failed:
            msg += f" {len(failed)} failed: " + "; ".join(f"{s.step_id} {s.error}" for s in failed)
        return self._end(res, "EXECUTED", msg, trace)

    def _apply_narrowing(self, intent: Intent, changes: list[dict], res: PipelineResult) -> dict | None:
        top = None
        for ch in changes:
            if "grain" in ch:
                intent.grain = ch["grain"]
                res.disclosures.append(f"Narrowed: {ch['grain']}ly grain.")
            if "days" in ch:
                intent.time_phrase = f"last {ch['days']} days"
                res.disclosures.append(f"Narrowed: last {ch['days']} days.")
            if "top" in ch:
                top = ch
        return top

    def _top_entities(self, top: dict, intent: Intent, filters: list[Filter], p: Principal) -> list[str]:
        window, _, _ = self._time(intent)
        r = self.gateway.run_spec(QuerySpec(metrics=["attempt_count"], dimensions=[top["dimension"]],
                                            filters=list(filters), time_range=(window.start, window.end),
                                            order_by=[("attempt_count", True)], limit=int(top["top"])), p)
        return [row[0] for row in r.rows if row[0] is not None] if r.ok else []

    def _run_step(self, step, p: Principal, cat: dict, window: TimeRange, trace) -> StepResult:
        sr = StepResult(step.id, step.purpose, "ERROR", step.period, step.dimension)
        if step.kind == "QUERY":
            try:
                r = self.gateway.run_spec(step.spec, p)
            except AccessDenied as e:
                sr.status, sr.error = "REFUSED_ACCESS", self.policy.refusal(self.model, e.auth)["message"]
                return sr
            v = r.validation
            sr.sql = v.rewritten_sql if v else r.sql
            sr.disclosures = list(v.disclosures) if v else []
            sr.rls = list(v.rls_predicates_applied) if v else []
            if v is not None and not v.ok:
                sr.status = {"FAIL_UNSAFE": "REJECTED_UNSAFE", "FAIL_ACCESS_DENIED": "REFUSED_ACCESS",
                             "NEEDS_NARROWING": "AWAITING_NARROWING"}.get(v.status, "ERROR")
                sr.error = v.user_message()
                return sr
            self._fill(sr, r)
            return sr
        # SQL step: the LLM writes it, the gate checks it, errors go back for repair
        out = self.sqlgen.run(step.request, p, cat, (window.start, window.end),
                              context={"spec": step.spec, "model": self.model}, trace=trace)
        sr.attempts = [a.__dict__ for a in out.attempts]
        sr.disclosures = out.assumptions + (list(out.validation.disclosures) if out.validation else [])
        if out.result is None:
            sr.status, sr.error = out.status if out.status != "OK" else "ERROR", out.message
            sr.sql = out.attempts[-1].sql if out.attempts else None
            return sr
        sr.rls = list(out.validation.rls_predicates_applied)
        sr.sql = out.validation.rewritten_sql
        self._fill(sr, out.result)
        return sr

    @staticmethod
    def _fill(sr: StepResult, r) -> None:
        sr.status = r.status
        sr.columns, sr.rows, sr.row_count = r.columns, r.rows, r.row_count
        sr.truncated, sr.duration_ms, sr.data_version, sr.error = r.truncated, r.duration_ms, r.data_version, r.error

    def _merchant_count(self, p: Principal) -> int:
        r = self.gateway.run_sql("SELECT COUNT(DISTINCT merchant_id) FROM dim_merchant", p)
        return int(r.rows[0][0]) if r.ok else 1000

    def _definitions(self, intent: Intent, p: Principal) -> str:
        cat = {m["name"]: m for m in self.catalog(p)["metrics"]}
        parts = []
        for ref in intent.metrics or [a["candidates"][0] for a in intent.ambiguous_terms]:
            m = cat.get(ref.partition(":")[0])
            if not m:
                continue
            s = f"{m['name']}: {m['description']}"
            for v in m.get("variants", []):
                s += f"\n  - {v['name']}{' (default)' if v['default'] else ''}: {v['description']}"
            parts.append(s)
        return "\n".join(parts) or "That metric isn't in the catalog you can access."

    def _end(self, res: PipelineResult, state: str, message: str, trace, t0=None) -> PipelineResult:
        res.state, res.message = state, message
        trace("DONE", state, None, None)
        return res


# --------------------------------------------------------------------------- offline handlers
def _offline_sql(ctx: dict) -> dict:
    spec, model = ctx.get("spec"), ctx.get("model")
    if spec is None or model is None:
        raise LLMError("the offline backend can't write custom SQL; set ANTHROPIC_API_KEY to enable it")
    q = model.compile(spec)
    from .sqlsafety import bind_params
    return {"sql": bind_params(q.sql, q.params), "tables_used": list(q.tables.values()),
            "metrics_used": [m.ref for m in q.metrics], "assumptions": [], "expected_columns": []}


OFFLINE_HANDLERS = {"parse": offline_parse, "sql": _offline_sql}
