"""PLANNING stage (SPEC 8.3): intent -> an analysis plan of query steps.

Planning is deterministic: each question type has a template, and every query
step is a QuerySpec that the semantic compiler turns into exact metric SQL. The
LLM only writes SQL for steps the metric layer cannot express (`custom_request`
-> a SQL step), and that SQL still goes through the same safety gate.

COMPUTE and DQ_CHECK steps are declared here and executed by the analysis engine
(segment 5).

The planner also guards result size: a plan whose breakdown would return more
rows than `max_result_rows` (e.g. hourly x every merchant x a year) is not run;
the user gets concrete ways to narrow it instead (SPEC 10.4).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from .semantic import Filter, QuerySpec, SemanticModel
from .timeparse import TimeRange

# a filter on the key implies a single value of these (no point breaking down by them)
IMPLIES = {"sub_method": ("payment_method",), "card_network": ("payment_method",), "flow": ("payment_method",),
           "merchant": ("merchant_id",), "merchant_id": ("merchant",), "issuer_bank": ("issuer_type",)}
TIME_DIMS = {"hour": ["day", "hour"], "day": ["day"], "week": ["week"], "month": ["month"]}


@dataclass
class Step:
    id: str
    kind: str                         # QUERY | SQL | COMPUTE | DQ_CHECK
    purpose: str
    spec: QuerySpec | None = None
    period: str | None = None         # P0 (baseline) | P1 (focus)
    request: str | None = None        # SQL steps: what to compute, in words
    method: str | None = None         # COMPUTE steps
    inputs: list[str] = field(default_factory=list)
    dimension: str | None = None
    role: str | None = None           # headline | trend | breakdown | ranking | reconcile | custom
    deferred: bool = False            # not executed (kept for plans that declare optional steps)

    def as_dict(self) -> dict:
        d = {"id": self.id, "kind": self.kind, "purpose": self.purpose, "period": self.period,
             "role": self.role}
        if self.spec:
            s = self.spec
            d["spec"] = {"metrics": s.metrics, "dimensions": s.dimensions,
                         "filters": [f.__dict__ for f in s.filters],
                         "time_range": [s.time_range[0].isoformat(), s.time_range[1].isoformat()] if s.time_range
                         else None, "time_basis": s.time_basis, "order_by": s.order_by, "limit": s.limit}
        for k in ("request", "method", "dimension"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.inputs:
            d["inputs"] = self.inputs
        return d


@dataclass
class Plan:
    plan_id: str
    objective: str
    question_type: str
    metrics: list[str]
    periods: dict[str, list[str]]
    steps: list[Step]
    candidate_dimensions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def query_steps(self) -> list[Step]:
        return [s for s in self.steps if s.kind in ("QUERY", "SQL") and not s.deferred]

    def as_dict(self) -> dict:
        return {"plan_id": self.plan_id, "objective": self.objective, "question_type": self.question_type,
                "metrics": self.metrics, "periods": self.periods, "steps": [s.as_dict() for s in self.steps],
                "candidate_dimensions": self.candidate_dimensions, "notes": self.notes}


@dataclass
class Narrowing:
    message: str
    estimated_rows: int
    options: list[dict]


class Planner:
    def __init__(self, model: SemanticModel, settings: dict):
        self.model = model
        self.d = settings.get("defaults", {})
        self.max_rows = settings["warehouse"]["max_result_rows"]
        self.max_queries = int(self.d.get("max_queries_per_question", 20))

    def _entity(self, ref: str) -> str:
        return self.model.resolve_metric(ref).entity

    def _dims_ok(self, dims: list[str], ref: str, catalog_dims: set[str]) -> list[str]:
        ent = self._entity(ref)
        return [d for d in dims if d in catalog_dims and self.model.dimension_available(d, ent)]

    def build(self, intent, refs: list[str], window: TimeRange, baseline: TimeRange | None,
              filters: list[Filter], catalog: dict) -> Plan:
        cat_dims = {d["name"] for d in catalog["dimensions"]}
        qt = intent.question_type
        primary = refs[0]
        notes: list[str] = []
        steps: list[Step] = []
        n = 0

        def sid():
            nonlocal n
            n += 1
            return f"s{n}"

        def spec(metrics, dims=(), tr=window, order=None, limit=None, basis=None):
            return QuerySpec(metrics=list(metrics), dimensions=list(dims), filters=list(filters),
                             time_range=(tr.start, tr.end), time_basis=basis, order_by=order or [], limit=limit)

        dims = self._dims_ok(intent.dimensions, primary, cat_dims)
        dropped = [d for d in intent.dimensions if d not in dims]
        if dropped:
            notes.append(f"Breakdown by {', '.join(dropped)} is not available for "
                         f"{primary.partition(':')[0]} with your access; shown without it.")
        # one headline query per entity (metrics from different entities can't share a query)
        by_entity: dict[str, list[str]] = {}
        for r in refs:
            by_entity.setdefault(self._entity(r), []).append(r)

        periods = {"P1": [window.start.isoformat(), window.end.isoformat()]}
        if baseline:
            periods["P0"] = [baseline.start.isoformat(), baseline.end.isoformat()]

        def headline(tr, period, purpose):
            for ent, ms in by_entity.items():
                steps.append(Step(sid(), "QUERY", purpose + (f" ({ent})" if len(by_entity) > 1 else ""),
                                  spec(self.with_parts(ms), tr=tr), period=period, role="headline"))

        grain = intent.grain if intent.grain != "none" else ("day" if window.days <= 92 else "week")

        if intent.custom_request:
            steps.append(Step(sid(), "SQL", intent.custom_request, request=intent.custom_request, period="P1",
                              role="custom"))
        elif qt == "RECONCILIATION":
            for r in refs:
                ent = self.model.entities[self._entity(r)]
                bases = list(ent.time_columns) if ent.time_ambiguous else [None]
                for b in bases:
                    steps.append(Step(sid(), "QUERY", f"{r} over the period" + (f" by {b}" if b else ""),
                                      spec([r], basis=b), period="P1", role="reconcile"))
            steps.append(Step(sid(), "COMPUTE", "Attribute the gap between sources", method="RECONCILE",
                              inputs=[s.id for s in steps]))
        elif qt == "DRIVER_ANALYSIS":
            headline(window, "P1", "Headline metric, focus period")
            if baseline:
                headline(baseline, "P0", "Headline metric, baseline period")
            span = TimeRange(baseline.start if baseline else window.start, window.end, "trend", "")
            steps.append(Step(sid(), "QUERY", f"{grain.title()} trend for the chart",
                              spec([primary], TIME_DIMS["day" if span.days <= 92 else "week"], tr=span), role="trend"))
            filtered = {f.dimension for f in filters if f.op in ("=",)}
            filtered |= {parent for f in filtered for parent in IMPLIES.get(f, ())}
            cands = dims + [d for d in self.d.get("driver_dimensions", []) if d not in dims]
            cands = [d for d in self._dims_ok(cands, primary, cat_dims) if d not in filtered]
            per_dim = 2 if baseline else 1
            budget = max(0, self.max_queries - len(steps))
            chosen = cands[: budget // per_dim]
            for d in chosen:
                ids = []
                for tr, p in ((window, "P1"), (baseline, "P0")):
                    if tr is None:
                        continue
                    s = Step(sid(), "QUERY", f"{primary} by {d}, {'focus' if p == 'P1' else 'baseline'} period",
                             spec(self.with_parts([primary]), [d], tr=tr), period=p, dimension=d, role="breakdown")
                    steps.append(s)
                    ids.append(s.id)
                steps.append(Step(sid(), "COMPUTE", f"Mix vs rate decomposition by {d}",
                                  method="MIX_RATE_DECOMPOSITION" if self.model.is_ratio(primary)
                                  else "CONTRIBUTION", inputs=ids, dimension=d))
            if len(chosen) < len(cands):
                notes.append(f"Query budget reached; not analysed: {', '.join(cands[len(chosen):])}.")
            steps.append(Step(sid(), "COMPUTE", "Rank drivers, drill into the top one", method="RANK_DRIVERS",
                              inputs=[s.id for s in steps if s.method in ("MIX_RATE_DECOMPOSITION", "CONTRIBUTION")]))
            steps.append(Step(sid(), "DQ_CHECK", "Data quality over the analysed window"))
            return Plan(uuid.uuid4().hex[:12], f"Explain the change in {primary}", qt, refs, periods, steps,
                        cands, notes + intent.notes)
        elif qt == "TREND":
            tdims = TIME_DIMS[grain] + [d for d in dims if d not in TIME_DIMS[grain]]
            for ent, ms in by_entity.items():
                steps.append(Step(sid(), "QUERY", f"{', '.join(ms)} by {grain}" +
                                  (f" and {', '.join(dims)}" if dims else ""), spec(ms, tdims), period="P1",
                                  role="trend"))
        elif qt == "RANKING" and dims:
            r = intent.ranking or {}
            rm = r.get("metric") if r.get("metric") in {x.partition(":")[0] for x in refs} else None
            order_ref = next((x for x in refs if x.partition(":")[0] == rm), primary)
            desc = r.get("descending", True)
            limit = r.get("limit") or 10
            ent_refs = by_entity[self._entity(order_ref)]
            steps.append(Step(sid(), "QUERY", f"{'Top' if desc else 'Bottom'} {limit} {dims[0]} by {order_ref}",
                              spec(ent_refs, dims, order=[(order_ref.partition(":")[0], desc)], limit=limit),
                              period="P1", role="ranking"))
            if baseline:
                steps.append(Step(sid(), "QUERY", "Same ranking, baseline period",
                                  spec(ent_refs, dims, tr=baseline, order=[(order_ref.partition(":")[0], desc)],
                                       limit=limit), period="P0", role="ranking"))
        else:   # LOOKUP, DISTRIBUTION, COMPARISON
            headline(window, "P1", "Headline metric")
            if baseline:
                headline(baseline, "P0", "Headline metric, baseline period")
            if dims:
                for ent, ms in by_entity.items():
                    steps.append(Step(sid(), "QUERY", f"{', '.join(ms)} by {', '.join(dims)}",
                                      spec(self.with_parts(ms), dims), period="P1", dimension=dims[0],
                                      role="breakdown"))
                    if baseline:
                        steps.append(Step(sid(), "QUERY", f"{', '.join(ms)} by {', '.join(dims)}, baseline",
                                          spec(self.with_parts(ms), dims, tr=baseline), period="P0",
                                          dimension=dims[0], role="breakdown"))
        steps.append(Step(sid(), "DQ_CHECK", "Data quality over the window"))
        obj = intent.intent_summary or f"{qt.lower()} of {primary}"
        return Plan(uuid.uuid4().hex[:12], obj, qt, refs, periods, steps, [], notes + intent.notes)

    def with_parts(self, refs: list[str]) -> list[str]:
        """Ratio metrics travel with their numerator and denominator: exact decompositions,
        significance tests and low-volume flags need the counts, not just the rate."""
        out = list(refs)
        for r in refs:
            if self.model.is_ratio(r):
                out += [f"{r}#num", f"{r}#den"]
        return out

    # ------------------------------------------------------------------ result-size guard
    def check_size(self, plan: Plan, catalog: dict, merchant_count: int) -> Narrowing | None:
        values = catalog.get("dimension_values", {})

        def card(d: str, tr: tuple[date, date]) -> int:
            if d == "day":
                return (tr[1] - tr[0]).days + 1
            if d == "week":
                return (tr[1] - tr[0]).days // 7 + 1
            if d == "month":
                return (tr[1] - tr[0]).days // 28 + 1
            if d in ("merchant", "merchant_id"):
                return max(merchant_count, 1)
            if d in values:
                return max(len(values[d]), 1)
            return 200     # unknown high-cardinality (city): conservative

        worst, worst_step, worst_fixed = 0, None, {}
        for s in plan.query_steps():
            if not s.spec or s.spec.limit:
                continue
            est = 1
            fixed = {f.dimension: (len(f.value) if isinstance(f.value, (list, tuple)) else 1)
                     for f in s.spec.filters if f.op in ("=", "in")}
            for d in s.spec.dimensions:
                est *= fixed.get(d) or card(d, s.spec.time_range)
            if est > worst:
                worst, worst_step, worst_fixed = est, s, fixed
        if worst <= self.max_rows:
            return None
        sp = worst_step.spec
        days = (sp.time_range[1] - sp.time_range[0]).days + 1
        opts = []
        if any(d in ("day", "hour") for d in sp.dimensions):
            opts.append({"id": "a", "label": "Weekly instead of daily/hourly over the same period",
                         "change": {"grain": "week"}})
        if days > 30:
            opts.append({"id": chr(97 + len(opts)), "label": "Only the last 30 days", "change": {"days": 30}})
        hc = [d for d in sp.dimensions if d in ("merchant", "merchant_id", "city") and d not in worst_fixed]
        if hc:
            opts.append({"id": chr(97 + len(opts)), "label": f"Only the top 20 {hc[0]}s by volume",
                         "change": {"top": 20, "dimension": hc[0]}})
        dims = " x ".join(f"{d} ({worst_fixed.get(d) or card(d, sp.time_range):,})" for d in sp.dimensions)
        return Narrowing(f"This would return about {worst:,} rows ({dims}), more than the {self.max_rows:,} "
                         f"I can analyse in one answer. How should I narrow it?", worst, opts)


def default_window(as_of: date, days: int) -> TimeRange:
    return TimeRange(as_of - timedelta(days=days - 1), as_of, f"last {days} days (default)", "",
                     note=f"No period given; used the last {days} days ending {as_of}.")
