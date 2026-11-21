"""Analysis engine (SPEC section 12) and source reconciliation (section 14).

Works only on executed, scoped query results: anything extra it needs (drill-downs,
failure codes, reconciliation tests) goes through the same safety gate as the user,
via the callbacks the pipeline passes in, and shows up as extra steps.

Methods:
  HEADLINE                 P0 / P1 / delta, two-proportion test for rates
  MIX_RATE_DECOMPOSITION   exact, symmetric; per-segment mix effect centred on the average
                           rate so each segment's number is interpretable on its own
  CONTRIBUTION             additive metrics (GMV, counts): segment deltas and share of change
  RANK_DRIVERS             |contribution| x significance x volume weight, parent dims pruned
  DRILL_DOWN               mix-driven -> where the extra volume came from;
                           rate-driven -> next dimension, hours, failure codes (depth <= 3)
  TREND                    daily series + robust z-score anomalies
  RECONCILIATION           two sources, attribution waterfall, residual
Hypotheses and recommendations are templated from these patterns (12.5 / 12.6);
the answer composer (segment 6) phrases them.
"""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from .dq import SEVERITY, DataQuality, DQResult, worst
from .planner import IMPLIES, Plan
from .semantic import Filter, QuerySpec, SemanticModel

TIMEOUT_CODES = {"ACQ_TO", "GW_TO", "BANK_TO"}
ISSUER_DOWN_CODES = {"91", "96"}


# --------------------------------------------------------------------------- statistics
def two_prop_p(n1: float, d1: float, n0: float, d0: float) -> float | None:
    """Two-sided two-proportion z-test p-value."""
    if not d0 or not d1:
        return None
    p0, p1 = n0 / d0, n1 / d1
    p = (n0 + n1) / (d0 + d1)
    se = math.sqrt(p * (1 - p) * (1 / d0 + 1 / d1))
    if se == 0:
        return 1.0 if p0 == p1 else 0.0
    z = (p1 - p0) / se
    return math.erfc(abs(z) / math.sqrt(2))


@dataclass
class Segment:
    segment: str
    w0: float
    w1: float
    r0: float | None
    r1: float | None
    num0: float
    den0: float
    num1: float
    den1: float
    mix_effect_pp: float
    rate_effect_pp: float
    total_pp: float
    share_of_change: float | None
    p_value: float | None
    significant: bool
    low_volume: bool
    new_or_gone: bool = False


@dataclass
class Decomposition:
    method: str                       # MIX_RATE_DECOMPOSITION | CONTRIBUTION
    dimension: str
    overall: dict
    segments: list
    totals: dict
    interpretation_code: str
    explained_share: float            # share of the change explained by the top aligned segment
    volume_shift: list = field(default_factory=list)
    concentration: float = 0.0        # best (share of change - share of volume) among aligned segments

    @property
    def quality(self) -> float:
        """How well this dimension explains the change. A mix-dominant split explains it away completely
        (every segment's own rate is flat), so it scores by the mix share; otherwise by how
        disproportionate the top segment is (a big segment absorbing a broad drop explains little)."""
        if self.interpretation_code == "MIX_DOMINANT":
            d = self.overall.get("delta_pp") or 0
            return min(abs(self.totals["mix_effect_pp"] / d), 1.0) if d else 0.0
        return self.concentration

    def top(self, n: int = 5) -> list:
        return self.segments[:n]

    def as_dict(self) -> dict:
        d = asdict(self)
        d["segments"] = [asdict(s) if not isinstance(s, dict) else s for s in self.segments]
        return d


def decompose_rate(dim: str, p0: dict, p1: dict, min_volume: float, alpha: float) -> Decomposition:
    """p0/p1: segment -> (numerator, denominator). Exact: mix + rate == delta (no residual)."""
    N0, D0 = sum(v[0] for v in p0.values()), sum(v[1] for v in p0.values())
    N1, D1 = sum(v[0] for v in p1.values()), sum(v[1] for v in p1.values())
    R0, R1 = (N0 / D0 if D0 else 0.0), (N1 / D1 if D1 else 0.0)
    Rbar, delta = (R0 + R1) / 2, R1 - R0
    segs = []
    for s in sorted(set(p0) | set(p1), key=str):
        n0, d0 = p0.get(s, (0, 0))
        n1, d1 = p1.get(s, (0, 0))
        w0, w1 = (d0 / D0 if D0 else 0), (d1 / D1 if D1 else 0)
        r0, r1 = (n0 / d0 if d0 else None), (n1 / d1 if d1 else None)
        gone = r0 is None or r1 is None
        a0 = r0 if r0 is not None else r1          # undefined rate: whole contribution is mix (exact, w=0)
        a1 = r1 if r1 is not None else r0
        a0, a1 = a0 or 0.0, a1 or 0.0
        mix = (w1 - w0) * ((a0 + a1) / 2 - Rbar) * 100
        rate = (a1 - a0) * (w0 + w1) / 2 * 100
        p = None if gone else two_prop_p(n1, d1, n0, d0)
        segs.append(Segment(str(s), w0, w1, r0, r1, n0, d0, n1, d1, mix, rate, mix + rate,
                            (mix + rate) / (delta * 100) if delta else None, p,
                            p is not None and p < alpha, min(d0, d1) < min_volume, gone))
    segs.sort(key=lambda x: -abs(x.total_pp))
    mix_t, rate_t = sum(x.mix_effect_pp for x in segs), sum(x.rate_effect_pp for x in segs)
    p_all = two_prop_p(N1, D1, N0, D0)
    d_pp = delta * 100
    if p_all is not None and p_all > alpha:
        code = "NO_SIGNIFICANT_CHANGE"
    elif d_pp and abs(mix_t) >= 0.7 * abs(d_pp):
        code = "MIX_DOMINANT"
    elif d_pp and abs(rate_t) >= 0.7 * abs(d_pp):
        code = "RATE_DOMINANT"
    else:
        code = "MIXED"
    aligned = [x for x in segs if d_pp and x.total_pp * d_pp > 0]
    explained = (aligned[0].total_pp / d_pp) if aligned else 0.0
    conc = max((x.total_pp / d_pp - (x.w0 + x.w1) / 2 for x in aligned), default=0.0)
    shift = sorted(segs, key=lambda x: -(x.w1 - x.w0))
    return Decomposition("MIX_RATE_DECOMPOSITION", dim,
                         {"P0": R0, "P1": R1, "delta_pp": d_pp, "p_value": p_all, "den_p0": D0, "den_p1": D1},
                         segs, {"mix_effect_pp": mix_t, "rate_effect_pp": rate_t,
                                "residual_pp": d_pp - mix_t - rate_t}, code, explained,
                         [{"segment": x.segment, "share_p0": x.w0, "share_p1": x.w1,
                           "share_change_pp": (x.w1 - x.w0) * 100} for x in shift[:3]], conc)


def contribution(dim: str, p0: dict, p1: dict, min_change_pct: float = 1.0) -> Decomposition:
    """Additive metric: segment deltas sum exactly to the total delta."""
    V0, V1 = sum(p0.values()), sum(p1.values())
    delta = V1 - V0
    segs = []
    for s in sorted(set(p0) | set(p1), key=str):
        v0, v1 = p0.get(s, 0.0), p1.get(s, 0.0)
        segs.append({"segment": str(s), "value_p0": v0, "value_p1": v1, "delta": v1 - v0,
                     "pct_change": ((v1 - v0) / v0 * 100) if v0 else None,
                     "share_of_change": (v1 - v0) / delta if delta else None,
                     "share_p0": v0 / V0 if V0 else None, "share_p1": v1 / V1 if V1 else None})
    segs.sort(key=lambda x: -abs(x["delta"]))
    pct = delta / V0 * 100 if V0 else None
    aligned = [x for x in segs if delta and x["delta"] * delta > 0]
    explained = aligned[0]["delta"] / delta if aligned else 0.0
    conc = max((x["delta"] / delta - ((x["share_p0"] or 0) + (x["share_p1"] or 0)) / 2 for x in aligned),
               default=0.0)
    code = "NO_SIGNIFICANT_CHANGE" if pct is not None and abs(pct) < min_change_pct else \
        "CONCENTRATED" if explained >= 0.5 else "BROAD"
    return Decomposition("CONTRIBUTION", dim, {"P0": V0, "P1": V1, "delta": delta, "pct_change": pct},
                         segs, {"delta": delta}, code, explained, [], conc)


# --------------------------------------------------------------------------- results
@dataclass
class Hypothesis:
    id: str
    code: str
    text: str
    confidence: str
    evidence: dict = field(default_factory=dict)


@dataclass
class Recommendation:
    id: str
    text: str
    follows_from: str
    expected_impact_pp: float | None = None


@dataclass
class AnalysisResult:
    question_type: str
    metric: str | None = None
    metric_format: str | None = None
    better: str | None = None
    headline: dict | None = None
    decompositions: list = field(default_factory=list)
    best_dimension: str | None = None
    interpretation_code: str | None = None
    drivers: list = field(default_factory=list)
    offsets: list = field(default_factory=list)
    drilldowns: list = field(default_factory=list)
    failure_codes: dict | None = None
    hour_window: dict | None = None
    trend: dict | None = None
    breakdown: dict | None = None
    ranking: dict | None = None
    reconciliation: dict | None = None
    dq: list = field(default_factory=list)
    dq_severity: str = "OK"
    dq_block: bool = False
    hypotheses: list = field(default_factory=list)
    recommendations: list = field(default_factory=list)
    caveats: list = field(default_factory=list)
    premise: dict | None = None
    events: list = field(default_factory=list)
    volume_shift: list = field(default_factory=list)

    def decomposition(self, dim: str) -> Decomposition | None:
        return next((d for d in self.decompositions if d.dimension == dim), None)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["decompositions"] = [x.as_dict() for x in self.decompositions]
        d["dq"] = [x.as_dict() if isinstance(x, DQResult) else x for x in self.dq]
        return d


def _col(ref: str) -> str:
    return ref.replace(":", "__").replace("#", "__")


def _num(v):
    return float(v) if v is not None else None


class Analyzer:
    def __init__(self, model: SemanticModel, settings: dict, dq: DataQuality):
        self.model = model
        d = settings.get("defaults", {})
        self.min_volume = float(d.get("min_segment_volume", 500))
        self.alpha = float(d.get("significance_alpha", 0.05))
        self.top_n = int(d.get("top_n_drivers", 5))
        self.max_depth = int(d.get("max_drill_depth", 3))
        self.drill_dims = list(d.get("drill_dimensions", ["issuer_bank", "acquirer", "hour", "device_type",
                                                          "payment_method", "sub_method", "merchant_segment"]))
        self.dq = dq

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _rows(sr) -> list[dict]:
        if sr is None or sr.status not in ("OK", "TRUNCATED"):
            return []
        names = [c["name"] for c in sr.columns]
        return [dict(zip(names, r)) for r in sr.rows]

    def _seg_parts(self, sr, dim: str, ref: str) -> dict:
        out = {}
        for r in self._rows(sr):
            k = r.get(dim)
            key = "Unknown" if k is None else k
            if self.model.is_ratio(ref):
                n, d_ = _num(r.get(_col(ref + "#num"))) or 0.0, _num(r.get(_col(ref + "#den"))) or 0.0
                pn, pd_ = out.get(key, (0.0, 0.0))
                out[key] = (pn + n, pd_ + d_)
            else:
                out[key] = out.get(key, 0.0) + (_num(r.get(_col(ref))) or 0.0)
        return out

    def _seg_values(self, sr, dim: str, col_ref: str) -> dict:
        out: dict = {}
        for r in self._rows(sr):
            k = "Unknown" if r.get(dim) is None else r.get(dim)
            out[k] = out.get(k, 0.0) + (_num(r.get(_col(col_ref))) or 0.0)
        return out

    def _decompose(self, dim, ref, sr0, sr1) -> Decomposition | None:
        p0, p1 = self._seg_parts(sr0, dim, ref), self._seg_parts(sr1, dim, ref)
        if not p0 or not p1:
            return None
        if self.model.is_ratio(ref):
            return decompose_rate(dim, p0, p1, self.min_volume, self.alpha)
        return contribution(dim, p0, p1)

    # ------------------------------------------------------------------ entry
    def analyze(self, res, plan: Plan, run, run_sql, principal) -> AnalysisResult:
        """`run(spec, purpose, period, dimension)` and `run_sql(sql, purpose)` execute through the gate
        as the user and return StepResults (also appended to the pipeline's steps)."""
        ref = res.metrics[0] if res.metrics else None
        met = self.model.metrics[ref.partition(":")[0]] if ref else None
        out = AnalysisResult(plan.question_type, ref, met.format if met else None, met.better if met else None)
        steps = {s.id: s for s in plan.steps}
        results = {sr.step_id: sr for sr in res.steps}
        by_role = lambda role: [(steps[k], v) for k, v in results.items()  # noqa: E731
                                if k in steps and steps[k].role == role]

        out.headline = self._headline(ref, by_role("headline")) if ref else None
        span0 = res.baseline.start if res.baseline else res.window.start
        dims = {s.spec.dimensions[0] for s in plan.steps if s.spec and s.spec.dimensions} | \
            {f["dimension"] for f in (res.intent.filters if res.intent else [])}
        out.dq = self.dq.run(span0, res.window.end, dims)
        out.events = self._events(span0, res.window.end)

        if plan.question_type == "RECONCILIATION":
            out.reconciliation = self.reconcile(res, by_role("reconcile"), run_sql)
            if out.reconciliation and abs(out.reconciliation["diff_pct"]) > 1:
                out.dq.append(DQResult("DQ09_SOURCE_MISMATCH", "finance_daily_settlement_summary",
                                       out.reconciliation["window"], "WARN",
                                       round(out.reconciliation["diff_pct"], 3), 1.0,
                                       "Dashboard GMV and the finance ledger differ by "
                                       f"{out.reconciliation['diff_pct']:.2f}%; see the reconciliation."))
        if plan.question_type == "DRIVER_ANALYSIS" and ref:
            self._drivers(out, ref, by_role("breakdown"), res, run)
        elif ref:
            br = by_role("breakdown")
            if br:
                out.breakdown = self._breakdown(ref, br)
                p0 = [x for x in br if x[0].period == "P0"]
                p1 = [x for x in br if x[0].period == "P1"]
                if p0 and p1:
                    dec = self._decompose(p1[0][0].dimension, ref, p0[0][1], p1[0][1])
                    if dec:
                        out.decompositions.append(dec)
                        out.best_dimension, out.interpretation_code = dec.dimension, dec.interpretation_code
            rk = by_role("ranking")
            if rk:
                out.ranking = self._ranking(ref, rk, plan)
        tr = by_role("trend")
        if tr and ref:
            out.trend = self._trend(ref, tr[0][1])

        used_dims = {d.dimension for d in out.decompositions} | dims
        out.dq_severity = worst(out.dq, used_dims)
        out.dq_block = any(r.severity == "BLOCK" and (r.affects is None or set(r.affects) & used_dims)
                           for r in out.dq)
        self._premise(out, res)
        self._hypotheses(out, res)
        self._caveats(out)
        return out

    # ------------------------------------------------------------------ methods
    def _headline(self, ref: str, steps) -> dict | None:
        vals = {}
        for st, sr in steps:
            rows = self._rows(sr)
            if not rows or _col(ref) not in rows[0]:
                continue
            r = rows[0]
            vals[st.period] = {"value": _num(r[_col(ref)]), "num": _num(r.get(_col(ref + "#num"))),
                               "den": _num(r.get(_col(ref + "#den")))}
        if "P1" not in vals:
            return None
        h = {"metric": ref, "P1": vals["P1"]["value"], "P0": vals.get("P0", {}).get("value"),
             "den_p1": vals["P1"]["den"], "den_p0": vals.get("P0", {}).get("den")}
        if h["P0"] is not None and h["P1"] is not None:
            h["delta"] = h["P1"] - h["P0"]
            h["pct_change"] = (h["delta"] / h["P0"] * 100) if h["P0"] else None
            if self.model.is_ratio(ref):
                h["delta_pp"] = h["delta"] * 100
                v0, v1 = vals["P0"], vals["P1"]
                if None not in (v0["num"], v0["den"], v1["num"], v1["den"]):
                    h["p_value"] = two_prop_p(v1["num"], v1["den"], v0["num"], v0["den"])
                    h["significant"] = h["p_value"] is not None and h["p_value"] < self.alpha
            else:
                h["significant"] = h["pct_change"] is not None and abs(h["pct_change"]) >= 1.0
            h["direction"] = "up" if h["delta"] > 0 else "down" if h["delta"] < 0 else "flat"
        return h

    def _drivers(self, out: AnalysisResult, ref: str, breakdowns, res, run) -> None:
        pairs: dict[str, dict] = {}
        for st, sr in breakdowns:
            pairs.setdefault(st.dimension, {})[st.period] = sr
        for dim, pr in pairs.items():
            if "P0" in pr and "P1" in pr:
                dec = self._decompose(dim, ref, pr["P0"], pr["P1"])
                if dec:
                    out.decompositions.append(dec)
        if not out.decompositions:
            return
        # a child dimension that explains as much as its parent is the better explanation
        by_dim = {d.dimension: d for d in out.decompositions}
        pruned = set()
        for child, parents in IMPLIES.items():
            for parent in parents:
                if child in by_dim and parent in by_dim and \
                        by_dim[child].quality >= 0.7 * by_dim[parent].quality:
                    pruned.add(parent)
        cands = [d for d in out.decompositions if d.dimension not in pruned] or out.decompositions
        # the best explanation is the most disproportionate one: a segment whose share of the change far
        # exceeds its share of volume (a big segment absorbing a broad drop explains little)
        best = max(cands, key=lambda d: d.quality)
        asked = [d for d in (res.intent.dimensions if res.intent else []) if d in by_dim]
        if asked:                         # "drill into X by merchant": answer by what was asked
            best = by_dim[asked[0]]
        out.best_dimension, out.interpretation_code = best.dimension, best.interpretation_code
        out.volume_shift = best.volume_shift
        if pruned:
            out.caveats.append(f"Breakdowns by {', '.join(sorted(pruned))} hide a finer mix shift; "
                               f"{best.dimension} explains the change better.")
        out.drivers, out.offsets = self._rank(cands, first=best.dimension)
        if out.drivers and out.interpretation_code != "NO_SIGNIFICANT_CHANGE":
            # drill-downs keep the question's own filters (e.g. CARD, card-not-present)
            base = list(breakdowns[0][0].spec.filters) if breakdowns and breakdowns[0][0].spec else []
            self._drill(out, ref, res, run, out.drivers[0], depth=1, used={d.dimension for d in out.decompositions},
                        filters=base)

    def _rank(self, decs: list[Decomposition], first: str | None = None) -> tuple[list, list]:
        drivers, offsets = [], []
        for d in decs:
            total = d.overall.get("delta_pp", d.overall.get("delta"))
            for s in d.segments:
                seg = s if isinstance(s, dict) else asdict(s)
                contrib = seg.get("total_pp", seg.get("delta"))
                if not total or contrib is None:
                    continue
                sig = seg.get("significant", True)
                low = seg.get("low_volume", False)
                vshare = ((seg.get("w0", seg.get("share_p0")) or 0) + (seg.get("w1", seg.get("share_p1")) or 0)) / 2
                lift = contrib / total - vshare
                row = {"dimension": d.dimension, "segment": seg["segment"], "contribution": contrib,
                       "share_of_change": contrib / total,
                       "mix_effect_pp": seg.get("mix_effect_pp"), "rate_effect_pp": seg.get("rate_effect_pp"),
                       "rate_p0": seg.get("r0"), "rate_p1": seg.get("r1"),
                       "share_p0": seg.get("w0", seg.get("share_p0")), "share_p1": seg.get("w1", seg.get("share_p1")),
                       "volume_p0": seg.get("den0"), "volume_p1": seg.get("den1"), "significant": sig, "low_volume": low,
                       "volume_share": vshare, "lift": lift,
                       "score": max(lift, 0.0) * (1.0 if sig else 0.5) * (0.3 if low else 1.0)}
                (drivers if contrib * total > 0 else offsets).append(row)
        drivers.sort(key=lambda r: (r["dimension"] != first, -r["score"]))
        offsets.sort(key=lambda r: -r["score"])
        return drivers[: self.top_n], offsets[:3]

    def _drill(self, out, ref, res, run, top: dict, depth: int, used: set, filters: list | None = None) -> None:
        if depth > self.max_depth or abs(top["share_of_change"]) < (0.5 if depth == 1 else 0.1):
            return
        base = list(filters or [])
        flt = base + [Filter(top["dimension"], "=", top["segment"])] if top["segment"] != "Unknown" else None
        if flt is None:
            return
        w, b = res.window, res.baseline
        if b is None:
            return
        mix_driven = top.get("mix_effect_pp") is not None and abs(top["mix_effect_pp"]) >= abs(top["rate_effect_pp"] or 0)
        label = f"{top['dimension']} = {top['segment']}"
        if mix_driven or not self.model.is_ratio(ref):
            # where did the extra volume come from?
            vol_ref = ref + "#den" if self.model.is_ratio(ref) else ref
            for dim in ("merchant", "merchant_segment"):
                if dim in used or not self.model.dimension_available(dim, self.model.resolve_metric(ref).entity):
                    continue
                srs = [run(QuerySpec(metrics=[vol_ref], dimensions=[dim], filters=flt, time_range=(t.start, t.end)),
                           f"Drill-down: volume of {label} by {dim}, {p}", p, dim) for t, p in ((b, "P0"), (w, "P1"))]
                v0, v1 = self._seg_values(srs[0], dim, vol_ref), self._seg_values(srs[1], dim, vol_ref)
                if not v1:
                    continue
                inc = {k: v1.get(k, 0) - v0.get(k, 0) for k in set(v0) | set(v1)}
                pos = sum(x for x in inc.values() if x > 0)
                ranked = sorted(inc.items(), key=lambda kv: -kv[1])
                out.drilldowns.append({
                    "depth": depth, "kind": "VOLUME", "parent": label, "dimension": dim,
                    "segments": [{"segment": str(k), "volume_p0": v0.get(k, 0), "volume_p1": v1.get(k, 0),
                                  "increase": x, "share_of_increase": (x / pos) if pos else None}
                                 for k, x in ranked[:5]]})
                break
            return
        # rate-driven: which next dimension / hours / failure codes explain the drop inside the segment
        ent = self.model.resolve_metric(ref).entity
        dims = [d for d in self.drill_dims if d not in used and d != top["dimension"]
                and self.model.dimension_available(d, ent)
                and not any(f.dimension == d for f in flt)][:3]
        decs = []
        for dim in dims:
            srs = [run(QuerySpec(metrics=[ref, ref + "#num", ref + "#den"], dimensions=[dim], filters=flt,
                                 time_range=(t.start, t.end)), f"Drill-down: {label} by {dim}, {p}", p, dim)
                   for t, p in ((b, "P0"), (w, "P1"))]
            dec = self._decompose(dim, ref, *srs)
            if dec:
                decs.append(dec)
                if dim == "hour" and w.days == 1:
                    out.hour_window = self._hour_window(dec, w.start)
        if decs:
            best = max(decs, key=lambda d: d.quality)
            out.drilldowns.append({"depth": depth, "kind": "RATE", "parent": label,
                                   "decompositions": [d.as_dict() for d in decs], "best_dimension": best.dimension})
        if depth == 1 and self.model.dimension_available("response_code", ent):
            out.failure_codes = self._failure_codes(flt, label, res, run, top)
        if decs and depth < self.max_depth:
            inner, _ = self._rank([max(decs, key=lambda d: d.quality)])
            if inner and inner[0]["dimension"] != "hour":
                self._drill(out, ref, res, run, inner[0], depth + 1, used | {d.dimension for d in decs}, flt)

    def _hour_window(self, dec: Decomposition, day: date) -> dict | None:
        drops = [(int(s.segment), (s.r1 or 0) - (s.r0 or 0)) for s in dec.segments
                 if s.segment.isdigit() and s.r0 is not None and s.r1 is not None and s.den1 >= 5]
        if not drops:
            return None
        worst_drop = min(d for _, d in drops)
        if worst_drop > -0.10:
            return None
        hours = sorted(h for h, d in drops if d <= min(worst_drop / 3, -0.15))
        start, end = hours[0], hours[-1] + 1
        return {"date": day.isoformat(), "start_hour": start, "end_hour": end,
                "label": f"{start:02d}:00-{end:02d}:00 IST", "worst_drop_pp": worst_drop * 100}

    def _failure_codes(self, flt, label, res, run, top) -> dict | None:
        w, b = res.window, res.baseline
        fl = flt + [Filter("status", "in", ["FAILED", "TIMEOUT"])]
        srs = [run(QuerySpec(metrics=["attempt_count"], dimensions=["response_code", "failure_reason"], filters=fl,
                             time_range=(t.start, t.end)), f"Failure codes for {label}, {p}", p, "response_code")
               for t, p in ((b, "P0"), (w, "P1"))]
        rows0, rows1 = self._rows(srs[0]), self._rows(srs[1])
        if not rows1:
            return None
        den0 = top.get("volume_p0") or sum(r["attempt_count"] for r in rows0) or 1
        den1 = top.get("volume_p1") or sum(r["attempt_count"] for r in rows1) or 1
        c0 = {(r["response_code"], r["failure_reason"]): r["attempt_count"] for r in rows0}
        c1 = {(r["response_code"], r["failure_reason"]): r["attempt_count"] for r in rows1}
        codes = []
        for k in set(c0) | set(c1):
            r0, r1 = c0.get(k, 0) / den0 if den0 else 0, c1.get(k, 0) / den1
            codes.append({"response_code": k[0], "failure_reason": k[1], "rate_p0": r0, "rate_p1": r1,
                          "change_pp": (r1 - r0) * 100, "count_p1": c1.get(k, 0)})
        codes.sort(key=lambda x: -x["change_pp"])
        return {"segment": label, "codes": codes[:5], "top_code": codes[0]["response_code"] if codes else None}

    def _breakdown(self, ref: str, steps) -> dict:
        st, sr = next(((s, r) for s, r in steps if s.period == "P1"), steps[0])
        dim = st.dimension or (sr.columns[0]["name"] if sr.columns else None)
        parts = self._seg_parts(sr, dim, ref)
        ratio = self.model.is_ratio(ref)
        total = sum(v[1] if ratio else v for v in parts.values()) or 1
        segs = []
        for k, v in parts.items():
            if ratio:
                segs.append({"segment": str(k), "value": v[0] / v[1] if v[1] else None, "volume": v[1],
                             "share": v[1] / total, "low_volume": v[1] < self.min_volume})
            else:
                segs.append({"segment": str(k), "value": v, "share": v / total})
        segs.sort(key=lambda x: -(x["value"] or 0))
        return {"dimension": dim, "segments": segs, "total": None if ratio else total}

    def _ranking(self, ref: str, steps, plan: Plan) -> dict:
        st, sr = next(((s, r) for s, r in steps if s.period == "P1"), steps[0])
        rows = self._rows(sr)
        dim = st.spec.dimensions[0] if st.spec and st.spec.dimensions else None
        base = {}
        p0 = next((r for s, r in steps if s.period == "P0"), None)
        for r in self._rows(p0):
            base[r.get(dim)] = r.get(_col(ref))
        ranked = [{"rank": i + 1, "segment": r.get(dim), "value": _num(r.get(_col(ref))),
                   "value_p0": _num(base.get(r.get(dim))) if base else None} for i, r in enumerate(rows)]
        return {"dimension": dim, "metric": ref, "rows": ranked,
                "order": "descending" if (st.spec.order_by and st.spec.order_by[0][1]) else "ascending"}

    def _trend(self, ref: str, sr) -> dict | None:
        rows = self._rows(sr)
        if not rows:
            return None
        tcol = sr.columns[0]["name"]
        series = [{"t": str(r[tcol]), "value": _num(r.get(_col(ref)))} for r in rows]
        vals = [p["value"] for p in series if p["value"] is not None]
        anomalies = []
        if len(vals) >= 7:
            med = statistics.median(vals)
            mad = statistics.median([abs(v - med) for v in vals]) or (abs(med) * 0.01 or 1)
            for p in series:
                if p["value"] is None:
                    continue
                z = (p["value"] - med) / (1.4826 * mad)
                if abs(z) > 3:
                    anomalies.append({"t": p["t"], "value": p["value"], "z": round(z, 1)})
        return {"grain": tcol, "series": series, "anomalies": anomalies[:10],
                "first": vals[0] if vals else None, "last": vals[-1] if vals else None}

    def _events(self, start: date, end: date) -> list[dict]:
        rows = self.dq._q("""SELECT MIN(date), MAX(date), COALESCE(event_name, holiday_name), BOOL_OR(is_sale_event)
                             FROM dim_date WHERE date BETWEEN ? AND ? AND COALESCE(event_name, holiday_name) IS NOT NULL
                             GROUP BY 3 ORDER BY 1""", start, end)
        return [{"name": n, "start": str(a), "end": str(b), "sale": bool(sale)} for a, b, n, sale in rows]

    # ------------------------------------------------------------------ reconciliation (section 14)
    def reconcile(self, res, steps, run_sql) -> dict | None:
        vals = {}
        for st, sr in steps:
            rows = self._rows(sr)
            if rows:
                m = st.spec.metrics[0]
                vals[(m, st.spec.time_basis)] = _num(list(rows[0].values())[0])
        dash = vals.get(("gmv_inr", None))
        fin = vals.get(("finance_gross_inr", None))
        if dash is None or fin is None:
            return None
        w = res.window
        lo, hi, hi1 = (int(d.strftime("%Y%m%d")) for d in (w.start, w.end, w.end + timedelta(days=1)))
        ts0, ts1 = w.start.isoformat(), (w.end + timedelta(days=1)).isoformat()
        utc = (f"a.created_at_utc >= TIMESTAMP '{ts0} 00:00:00' AND a.created_at_utc < TIMESTAMP '{ts1} 00:00:00' "
               f"AND a.date_key BETWEEN {lo} AND {hi1}")
        join = "FROM fact_settlements s JOIN v_attempts a ON a.attempt_id = s.attempt_id"

        def one(sql, purpose):
            sr = run_sql(sql, purpose)
            rows = self._rows(sr)
            return rows, sr
        st_rows, _ = one(f"SELECT s.settlement_status, SUM(s.gross_amount_paise) / 100.0 AS amount_inr {join} "
                         f"WHERE a.date_key BETWEEN {lo} AND {hi} AND s.settlement_date_key >= {lo} GROUP BY 1",
                         "Reconciliation: settlement amount by status (IST transaction day)")
        by_status = {r["settlement_status"]: _num(r["amount_inr"]) for r in st_rows}
        utc_rows, _ = one(f"SELECT SUM(s.gross_amount_paise) / 100.0 AS amount_inr {join} "
                          f"WHERE s.settlement_status = 'SETTLED' AND {utc} AND s.settlement_date_key >= {lo}",
                          "Reconciliation: settled amount by UTC transaction day (finance's day boundary)")
        settled_utc = _num(utc_rows[0]["amount_inr"]) if utc_rows else None
        miss_rows, _ = one(f"SELECT m.merchant_id, SUM(s.gross_amount_paise) / 100.0 AS amount_inr {join} "
                           f"JOIN dim_merchant m ON m.merchant_key = s.merchant_key "
                           f"WHERE s.settlement_status = 'SETTLED' AND {utc} AND s.settlement_date_key >= {lo} "
                           f"AND m.merchant_id NOT IN (SELECT DISTINCT merchant_id FROM finance_daily_settlement_summary "
                           f"WHERE summary_date BETWEEN DATE '{w.start}' AND DATE '{w.end}') GROUP BY 1 ORDER BY 2 DESC",
                           "Reconciliation: settled amount of merchants absent from the finance ledger")
        nos_rows, _ = one(f"SELECT SUM(a.amount_paise) / 100.0 AS amount_inr FROM v_attempts a "
                          f"LEFT JOIN fact_settlements s ON s.attempt_id = a.attempt_id AND s.settlement_date_key >= {lo} "
                          f"WHERE a.status = 'SUCCESS' AND a.date_key BETWEEN {lo} AND {hi} AND s.attempt_id IS NULL",
                          "Reconciliation: successful payments with no settlement record")
        if not st_rows or settled_utc is None:
            return None
        settled_ist = by_status.get("SETTLED", 0.0)
        missing = sum(_num(r["amount_inr"]) or 0 for r in miss_rows)
        factors = [
            ("NOT_YET_SETTLED_PENDING", by_status.get("PENDING", 0.0), "Payments not settled yet (PENDING)"),
            ("REVERSED_EXCLUDED_IN_FINANCE", by_status.get("REVERSED", 0.0), "Reversed settlements, excluded by finance"),
            ("SETTLEMENT_FAILED_EXCLUDED", by_status.get("FAILED", 0.0), "Failed settlements"),
            ("ON_HOLD_EXCLUDED", by_status.get("ON_HOLD", 0.0), "Settlements on hold"),
            ("NO_SETTLEMENT_RECORD", (_num(nos_rows[0]["amount_inr"]) or 0.0) if nos_rows else 0.0,
             "Successful payments without a settlement record"),
            ("TIMEZONE_DAY_BOUNDARY", settled_ist - settled_utc,
             "Finance books by UTC day, the dashboard by IST day (05:30 offset at the month edges)"),
            ("MERCHANTS_MISSING_IN_FINANCE", missing,
             f"Merchants absent from the finance ledger ({', '.join(r['merchant_id'] for r in miss_rows[:5])})"),
        ]
        diff = dash - fin
        explained = [{"factor": f, "amount_inr": round(a, 2), "explains_pct": a / dash * 100, "description": t}
                     for f, a, t in factors if abs(a) >= 0.005]
        resid = diff - sum(x["amount_inr"] for x in explained)
        ratio = fin / settled_utc if settled_utc else None
        return {
            "metric": "settled amount", "window": [w.start.isoformat(), w.end.isoformat()],
            "source_a": {"name": "dashboard GMV (v_attempts, IST day)", "value": dash},
            "source_b": {"name": "finance_daily_settlement_summary (UTC day)", "value": fin},
            "settled_ist": settled_ist, "settled_utc": settled_utc,
            "settled_by_settlement_date": vals.get(("settled_gmv_inr", "settlement_date")),
            "diff_inr": diff, "diff_pct": diff / dash * 100 if dash else None,
            "explained": sorted(explained, key=lambda x: -abs(x["amount_inr"])),
            "residual_inr": resid, "residual_pct": resid / dash * 100 if dash else None,
            "unit_check": {"finance_vs_settled_ratio": ratio,
                           "paise_rupee_error": bool(ratio and (90 <= ratio <= 110 or 0.009 <= ratio <= 0.011))},
            "recommendation": "Use the finance ledger for statutory and payout reporting, and the warehouse (IST "
                              "transaction day) for operational analysis. Align on the day boundary, the treatment of "
                              "reversed / on-hold settlements, and onboard the missing merchants to the ledger.",
        }

    # ------------------------------------------------------------------ interpretation
    def _premise(self, out: AnalysisResult, res) -> None:
        h = out.headline
        if not h or h.get("delta") is None:
            return
        q = res.question.lower()
        asked = "down" if re.search(r"\b(drop|dropped|decline|declined|fall|fell|decrease|worse|dip|down)\b", q) else \
            "up" if re.search(r"\b(rise|rose|increase|increased|grow|grew|improve|improved|up|spike)\b", q) else None
        if asked is None:
            return
        actual = h["direction"]
        sig = h.get("significant", True)
        out.premise = {"asked": asked, "actual": actual, "significant": sig,
                       "holds": asked == actual and sig}

    def _hypotheses(self, out: AnalysisResult, res) -> None:
        H, R = out.hypotheses, out.recommendations

        def hyp(code, text, conf, **ev):
            h = Hypothesis(f"H{len(H) + 1}", code, text, conf, ev)
            H.append(h)
            return h

        def rec(text, h, impact=None):
            R.append(Recommendation(f"R{len(R) + 1}", text, h.id, impact))

        fmt = lambda v: "n/a" if v is None else f"{v * 100:.1f}%"  # noqa: E731
        code = out.interpretation_code
        top = out.drivers[0] if out.drivers else None
        dq_ids = {r.check_id for r in out.dq}
        if out.premise and not out.premise["holds"]:
            word = "decline" if out.premise["asked"] == "down" else "rise"
            hp = out.headline or {}
            move = (f"{hp['delta_pp']:+.2f}pp" if hp.get("delta_pp") is not None
                    else f"{hp.get('pct_change') or 0:+.1f}%")
            why = (f"it went {out.premise['actual']} instead ({move})" if out.premise["actual"] != out.premise["asked"]
                   else f"the change ({move}) is within normal variation")
            hyp("PREMISE_NOT_SUPPORTED", f"The data does not show a meaningful {word}: {why}.", "HIGH",
                headline=out.headline)
        if "DQ05_LATE_ARRIVING" in dq_ids and res.window.days <= 2 and \
                res.window.end >= (self.dq.as_of - timedelta(days=1)):
            h = hyp("INCOMPLETE_DATA", "The most recent day(s) after the as-of date are still arriving; an apparent "
                    "drop there would be an artefact of incomplete data, not a real decline.", "HIGH")
            rec("Wait for data completeness before reporting the latest day.", h)
        if code == "NO_SIGNIFICANT_CHANGE":
            hyp("NO_SIGNIFICANT_CHANGE", "The change is within normal day-to-day variation (p = "
                f"{(out.headline or {}).get('p_value') or 0:.2f}).", "MEDIUM")
        vs = out.volume_shift[0] if out.volume_shift else None
        if code == "NO_SIGNIFICANT_CHANGE" and vs and vs["share_change_pp"] >= 10:
            hyp("VOLUME_SHIFT", f"The rate is stable, but volume shifted toward {vs['segment']} ({out.best_dimension}): "
                f"{vs['share_p0']:.1%} -> {vs['share_p1']:.1%} of attempts.", "HIGH", shift=out.volume_shift)
        if top and code in ("MIX_DOMINANT", "MIXED") and abs(top.get("mix_effect_pp") or 0) >= abs(top.get("rate_effect_pp") or 0):
            vol = next((d for d in out.drilldowns if d["kind"] == "VOLUME"), None)
            src = ""
            if vol and vol["segments"] and (vol["segments"][0]["share_of_increase"] or 0) >= 0.5:
                s0 = vol["segments"][0]
                src = f" Most of the extra {top['segment']} volume ({s0['share_of_increase']:.0%}) came from {s0['segment']}."
            h = hyp("MIX_SHIFT",
                    f"The change is driven by a traffic mix shift toward {top['segment']} ({top['dimension']}): its share "
                    f"went from {fmt(top['share_p0'])} to {fmt(top['share_p1'])} of volume and its success rate is below "
                    f"average ({fmt(top['rate_p1'])}), while its own rate barely moved ({fmt(top['rate_p0'])} -> "
                    f"{fmt(top['rate_p1'])}). This is not a deterioration in performance." + src,
                    "HIGH" if code == "MIX_DOMINANT" else "MEDIUM", driver=top, volume=vol)
            impact = -top["mix_effect_pp"] if top.get("mix_effect_pp") is not None else None
            if top["segment"] == "UPI_COLLECT":
                who = vol["segments"][0]["segment"] if vol and vol["segments"] else "the merchants driving the shift"
                rec(f"Move {who}'s UPI collect traffic to UPI intent (higher success rate).", h, impact)
            else:
                rec(f"Report success rate by {top['dimension']} so mix shifts are not read as performance drops; "
                    f"watch the {top['segment']} share.", h, impact)
        if top and code != "NO_SIGNIFICANT_CHANGE" and top.get("rate_effect_pp") is not None \
                and abs(top["rate_effect_pp"]) > abs(top.get("mix_effect_pp") or 0):
            fc = out.failure_codes or {}
            codes = fc.get("codes") or []
            c0 = codes[0] if codes else None
            hw = out.hour_window
            if top["dimension"] == "issuer_bank" and (hw or (c0 and str(c0["response_code"]) in ISSUER_DOWN_CODES)):
                when = f" between {hw['label']} on {hw['date']}" if hw else ""
                h = hyp("ISSUER_OUTAGE",
                        f"Possible issuer-side outage at {top['segment']}{when}: its success rate fell from "
                        f"{fmt(top['rate_p0'])} to {fmt(top['rate_p1'])}"
                        + (f", with failures dominated by code {c0['response_code']} ({c0['failure_reason']})"
                           if c0 else "") + ".", "HIGH" if hw and c0 else "MEDIUM", driver=top, hours=hw, codes=codes[:3])
                rec(f"Raise the incident with {top['segment']} and enable retry via an alternate method or issuer "
                    f"routing during its downtime.", h, -top["rate_effect_pp"])
            elif top["dimension"] == "acquirer" and c0 and (str(c0["response_code"]) in TIMEOUT_CODES
                                                            or "timeout" in str(c0["failure_reason"]).lower()):
                h = hyp("ACQUIRER_DEGRADATION",
                        f"{top['segment']} degraded: its success rate fell from {fmt(top['rate_p0'])} to "
                        f"{fmt(top['rate_p1'])}, and {c0['failure_reason'].lower()} failures rose from "
                        f"{fmt(c0['rate_p0'])} to {fmt(c0['rate_p1'])} of its attempts.", "HIGH", driver=top,
                        codes=codes[:3])
                rec(f"Shift routing away from {top['segment']} until its timeouts return to normal.", h,
                    -top["rate_effect_pp"])
            else:
                h = hyp("RATE_CHANGE_IN_SEGMENT",
                        f"Success rate changed within {top['segment']} ({top['dimension']}): {fmt(top['rate_p0'])} -> "
                        f"{fmt(top['rate_p1'])}" + (f"; the largest failure increase is {c0['failure_reason']} "
                                                     f"({c0['response_code']})" if c0 else "") + ".",
                        "MEDIUM", driver=top, codes=codes[:3])
                rec(f"Investigate {top['segment']} ({top['dimension']}) and set an alert on its success rate.", h,
                    -top["rate_effect_pp"])
        if "DQ10_SCD_CHANGE" in dq_ids and {"merchant_segment", "category"} & {d.dimension for d in out.decompositions}:
            r = next(x for x in out.dq if x.check_id == "DQ10_SCD_CHANGE")
            hyp("RECLASSIFICATION", f"Segment trends are affected by merchant reclassification on "
                f"{', '.join(r.dates or [])}.", "MEDIUM")
        anomalous = {d for r in out.dq if r.check_id == "DQ06_VOLUME_ANOMALY" for d in (r.dates or [])}
        for ev in out.events:
            if res.baseline is None or not (ev["sale"] or ev["start"] in anomalous):
                continue
            hyp("SEASONAL_EVENT", f"Seasonal load during {ev['name']} ({ev['start']} to {ev['end']}) may affect volumes "
                "and rates in the compared periods.", "LOW", event=ev)
        if out.reconciliation:
            rc = out.reconciliation
            top_f = rc["explained"][:3]
            h = hyp("SOURCE_DEFINITIONS",
                    "The two sources measure different things: " + "; ".join(
                        f"{f['description']} ({f['explains_pct']:.2f}%)" for f in top_f)
                    + f". Residual after these factors: {rc['residual_pct']:.3f}%.", "HIGH", reconciliation=True)
            rec(rc["recommendation"], h)
        if any(d.get("low_volume") for d in out.drivers[:3]):
            out.caveats.append("Some top segments have low volume; their rates are noisy.")

    def _caveats(self, out: AnalysisResult) -> None:
        for r in out.dq:
            if SEVERITY[r.severity] >= SEVERITY["WARN"]:
                out.caveats.append(f"[{r.check_id}] {r.impact}")
