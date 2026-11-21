"""AMBIGUITY_CHECK stage (SPEC section 9).

Deterministic rules decide *whether* something is ambiguous; cheap probe queries
(run through the safety gate as the asking user) decide whether it *matters*.
Material ambiguities become a clarification question with the probe values
shown; immaterial ones are assumed and disclosed.

Item keys are stable (`TYPE:field`) so a user's answer, stored in the session,
resolves the same item when the question is re-run.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta

from .semantic import Filter, QuerySpec, SemanticModel
from .timeparse import TimeRange


@dataclass
class Option:
    id: str
    label: str
    value: str                       # machine value: metric ref, dimension name, "days:14" ...
    estimated_value: float | None = None
    display: str | None = None


@dataclass
class AmbiguityItem:
    type: str                        # METRIC_DEFINITION | UNIT | TIME_WINDOW | ENTITY_RESOLUTION | ...
    field: str
    options: list[Option]
    recommended: str
    question_to_user: str
    materiality_pct: float | None = None

    @property
    def key(self) -> str:
        return f"{self.type}:{self.field}"

    def option(self, oid: str) -> Option | None:
        return next((o for o in self.options if o.id == oid), None)


@dataclass
class AmbiguityReport:
    items: list[AmbiguityItem] = field(default_factory=list)     # still open -> ask
    assumptions: list[str] = field(default_factory=list)         # resolved by default -> disclose
    choices: dict[str, str] = field(default_factory=dict)        # key -> chosen machine value

    @property
    def action(self) -> str:
        return "ASK" if self.items else "PROCEED"

    def as_dict(self) -> dict:
        return {"is_ambiguous": bool(self.items), "action": self.action,
                "items": [asdict(i) | {"key": i.key} for i in self.items],
                "assumptions": self.assumptions, "choices": self.choices}


def _fmt(v: float | None, fmt: str) -> str:
    if v is None:
        return "n/a"
    if fmt == "percent":
        return f"{v * 100:.1f}%"
    if fmt == "currency_inr":
        return f"₹{v / 1e7:,.2f} Cr" if abs(v) >= 1e7 else f"₹{v:,.0f}"
    if fmt == "integer":
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def _materiality(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    if len(vals) < 2 or not vals[0]:
        return None
    return max(abs(v - vals[0]) / abs(vals[0]) for v in vals[1:]) * 100


class AmbiguityDetector:
    def __init__(self, model: SemanticModel, gateway, settings: dict):
        self.model = model
        self.gateway = gateway
        self.cfg = settings.get("ambiguity", {})
        self.mode = self.cfg.get("mode", "ask_when_material")
        self.threshold = float(self.cfg.get("material_threshold_pct", 10))

    def _probe(self, ref: str, window: TimeRange, filters: list[Filter], principal) -> float | None:
        try:
            r = self.gateway.run_spec(QuerySpec(metrics=[ref], filters=filters,
                                                time_range=(window.start, window.end)), principal)
        except Exception:  # noqa: BLE001 - a probe the user can't run is simply not an option
            return None
        if not r.ok or not r.rows or r.rows[0][0] is None:
            return None
        return float(r.rows[0][0])

    def _decide(self, item: AmbiguityItem, report: AmbiguityReport, clarified: dict[str, str],
                force_default: bool, always_material: bool = False) -> None:
        if item.key in clarified and item.option(clarified[item.key]):
            o = item.option(clarified[item.key])
            report.choices[item.key] = o.value
            report.assumptions.append(f"{item.field}: {o.label} (your choice).")
            return
        material = always_material or (item.materiality_pct is not None and item.materiality_pct > self.threshold)
        ask = self.mode == "always_ask" or (self.mode == "ask_when_material" and material)
        if ask and not force_default:
            report.items.append(item)
            return
        o = item.option(item.recommended)
        report.choices[item.key] = o.value
        alts = "; ".join(f"{x.label} = {x.display}" for x in item.options if x.id != o.id and x.display)
        report.assumptions.append(f"{item.field}: assumed {o.label}" + (f" ({o.display})" if o.display else "")
                                  + (f". Alternatives: {alts}." if alts else "."))

    def detect(self, intent, catalog: dict, principal, window: TimeRange | None, filters: list[Filter],
               clarified: dict[str, str], force_default: bool = False) -> AmbiguityReport:
        rep = AmbiguityReport()
        cat_metrics = {m["name"]: m for m in catalog["metrics"]}
        probe_window = window

        # TIME_WINDOW: vague phrase ("lately") -> ask, with the metric shown for each option
        if window is not None and window.ambiguous:
            opts = []
            ref = intent.metrics[0] if intent.metrics else None
            for i, days in enumerate((7, 14, 30)):
                w = TimeRange(window.end - timedelta(days=days - 1), window.end, f"last {days} days", "")
                v = self._probe(ref, w, filters, principal) if ref else None
                fmt = self.model.metrics[ref.partition(":")[0]].format if ref else "decimal"
                opts.append(Option(chr(97 + i), f"Last {days} days ({w.start} to {w.end})", f"days:{days}", v,
                                   _fmt(v, fmt) if ref else None))
            self._decide(AmbiguityItem(
                "TIME_WINDOW", "time period", opts, "b",
                f"“{window.expression}” is vague. Which period should I use?"),
                rep, clarified, force_default, always_material=True)
            ch = rep.choices.get("TIME_WINDOW:time period")
            if ch:
                d = int(ch.split(":")[1])
                probe_window = TimeRange(window.end - timedelta(days=d - 1), window.end, f"last {d} days", "")

        # METRIC_DEFINITION: metrics with several definitions the user did not pick
        for ref in intent.metrics:
            name, _, variant = ref.partition(":")
            met = self.model.metrics[name]
            if variant or not met.has_variants or probe_window is None:
                continue
            visible = cat_metrics.get(name, {}).get("variants", [])
            vnames = [v["name"] for v in visible] or list(met.variants)
            vnames = [met.default_variant] + [v for v in vnames if v != met.default_variant]
            opts = []
            for i, v in enumerate(vnames):
                val = self._probe(f"{name}:{v}", probe_window, filters, principal)
                if val is None and i:
                    continue
                opts.append(Option(chr(97 + len(opts)), met.variants[v].description.replace(" (DEFAULT)", ""),
                                   f"{name}:{v}", val, _fmt(val, met.format)))
            if len(opts) < 2:
                continue
            item = AmbiguityItem(
                "METRIC_DEFINITION", name, opts, "a",
                f"“{intent.metric_terms.get(ref, name)}” has {len(opts)} definitions that give different answers "
                f"for {probe_window.label}: " + ", ".join(f"{o.label} = {o.display}" for o in opts) + ". Which one?",
                materiality_pct=_materiality([o.estimated_value for o in opts]))
            self._decide(item, rep, clarified, force_default)

        # Ambiguous business words ("revenue", "volume"): candidates come from the user's catalog only
        for amb in intent.ambiguous_terms:
            cands = amb["candidates"]
            metric_cands = [c for c in cands if c.partition(":")[0] in self.model.metrics]
            if not metric_cands:
                continue
            opts, fmts = [], set()
            for c in metric_cands:
                met = self.model.metrics[c.partition(":")[0]]
                fmts.add(met.format)
                val = self._probe(c, probe_window, filters, principal) if probe_window else None
                opts.append(Option(chr(97 + len(opts)), f"{met.name}: {met.description}", c, val,
                                   _fmt(val, met.format) if probe_window else None))
            rec = next(o.id for o in opts if o.value == amb.get("default")) if any(
                o.value == amb.get("default") for o in opts) else "a"
            item = AmbiguityItem(
                "UNIT" if len(fmts) > 1 else "METRIC_DEFINITION", amb["term"], opts, rec,
                f"“{amb['term']}” can mean different things here. " + (amb.get("note") or "") + " Which one?",
                materiality_pct=_materiality([o.estimated_value for o in opts]))
            self._decide(item, rep, clarified, force_default, always_material=len(fmts) > 1)
        return rep


def apply_choices(intent, report: AmbiguityReport, window: TimeRange | None) -> tuple[list[str], TimeRange | None]:
    """Metric refs and window after the user's (or default) choices."""
    refs = []
    for ref in intent.metrics:
        name = ref.partition(":")[0]
        refs.append(report.choices.get(f"METRIC_DEFINITION:{name}", ref))
    for amb in intent.ambiguous_terms:
        for t in ("METRIC_DEFINITION", "UNIT"):
            ch = report.choices.get(f"{t}:{amb['term']}")
            if ch and ch not in refs:
                refs.append(ch)
    tw = report.choices.get("TIME_WINDOW:time period")
    if tw and window is not None:
        d = int(tw.split(":")[1])
        window = TimeRange(window.end - timedelta(days=d - 1), window.end, f"last {d} days",
                           window.expression)
    return refs, window


def resolve_merchants(gateway, principal, names: list[str]) -> tuple[list[str], list[str], list[AmbiguityItem]]:
    """Resolve merchant names INSIDE the user's scope (the query runs through the gate with RLS),
    so a scoped user can never learn whether an out-of-scope merchant exists.
    Returns (resolved names, not found, entity-resolution questions)."""
    import difflib
    r = gateway.run_sql("SELECT DISTINCT merchant_name FROM dim_merchant WHERE is_current", principal)
    known = [row[0] for row in r.rows] if r.ok else []
    resolved, missing, items = [], [], []
    for n in names:
        nl = n.lower().strip()
        exact = [k for k in known if k.lower() == nl]
        if exact:
            resolved.append(exact[0])
            continue
        contains = [k for k in known if nl in k.lower()]
        scored = sorted(((difflib.SequenceMatcher(None, nl, k.lower()).ratio(), k) for k in known), reverse=True)
        cands = contains or [k for s, k in scored[:3] if s >= 0.75]
        if len(contains) == 1:                 # unique partial name ("QuickBazaar" -> "QuickBazaar Retail")
            resolved.append(contains[0])
        elif cands:                            # fuzzy or several matches: never substitute silently
            q = (f"Did you mean “{cands[0]}”?" if len(cands) == 1
                 else f"Several merchants match “{n}”. Which one?")
            items.append(AmbiguityItem("ENTITY_RESOLUTION", f"merchant “{n}”",
                                       [Option(chr(97 + i), c, c) for i, c in enumerate(cands[:4])], "a", q))
        else:
            missing.append(n)
    return resolved, missing, items
