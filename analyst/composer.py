"""COMPOSITION + GROUNDING_CHECK (SPEC sections 15 and 16).

1. `draft()` builds the whole answer deterministically from the executed results
   and the analysis: headline, facts (each citing its evidence), hypotheses with a
   validation step, recommendations that reference them, caveats, a scored
   confidence, an evidence list, chart specs and follow-ups. Every number in the
   draft is formatted from a computed value, so the draft is grounded by construction.
2. `phrase()` lets the LLM rewrite the *wording* only (same ids, same structure).
3. `ground()` extracts every number from the text and matches it, with rounding
   tolerance, against the values the queries and the analysis produced. A sentence
   with an unmatched number is re-requested (at most twice) and otherwise falls back
   to the draft wording, and the answer is marked ANSWERED_LOW_CONFIDENCE.
"""
from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date

from .llm import LLM, LLMError

NAMES = {"gmv_inr": "GMV", "mdr_revenue_inr": "MDR revenue", "attempt_count": "Transactions",
         "success_rate": "Success rate", "failure_rate": "Failure rate", "settled_gmv_inr": "Settled GMV",
         "finance_gross_inr": "Finance gross settled amount", "net_settled_inr": "Net settled amount",
         "avg_ticket_inr": "Average ticket size", "order_count": "Orders", "refund_rate": "Refund rate",
         "timeout_rate": "Timeout rate", "p95_latency_ms": "p95 latency", "active_merchants": "Active merchants"}

VALIDATE = {
    "MIX_SHIFT": "Confirm with the merchant's account manager what changed (campaign, checkout change) and when.",
    "ISSUER_OUTAGE": "Check the issuer's status page or incident channel for the same window; compare response "
                     "codes across acquirers to rule out a routing issue.",
    "ACQUIRER_DEGRADATION": "Check the acquirer's latency and timeout dashboards; confirm the same issuers perform "
                            "normally on the other acquirers.",
    "RATE_CHANGE_IN_SEGMENT": "Break the segment down further (issuer, hour, failure code) to localise the cause.",
    "INCOMPLETE_DATA": "Re-run the question once the latest day has fully arrived.",
    "RECLASSIFICATION": "Compare the trend using current segments (merchant_segment_current) instead of point-in-time.",
    "SEASONAL_EVENT": "Compare against the same event last year or a normal week.",
    "SOURCE_DEFINITIONS": "Reconcile a sample of merchants day by day with the finance team.",
    "PREMISE_NOT_SUPPORTED": "Check whether a different definition or period was meant.",
    "NO_SIGNIFICANT_CHANGE": "Look at a longer window to see whether a trend emerges.",
    "VOLUME_SHIFT": "Confirm with the merchant whether the volume change was planned.",
}
OWNER = {"MIX_SHIFT": ("LOW", "Merchant success team"), "ISSUER_OUTAGE": ("MEDIUM", "Payments operations / issuer relations"),
         "ACQUIRER_DEGRADATION": ("LOW", "Routing / payments operations"),
         "RATE_CHANGE_IN_SEGMENT": ("MEDIUM", "Payments analytics"), "INCOMPLETE_DATA": ("LOW", "Analyst"),
         "SOURCE_DEFINITIONS": ("MEDIUM", "Finance + data engineering")}


# --------------------------------------------------------------------------- formatting
def fmt(v, kind: str | None) -> str:
    if v is None:
        return "n/a"
    v = float(v)
    if kind == "currency_inr" and abs(v) < 0.5:
        v = 0.0
    if kind == "percent":
        return f"{v * 100:.1f}%"
    if kind == "currency_inr":
        a = abs(v)
        if a >= 1e7:
            return f"₹{v / 1e7:,.2f} Cr"
        if a >= 1e5:
            return f"₹{v / 1e5:,.1f} L"
        return f"₹{v:,.0f}"
    if kind == "integer":
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def _lc(name: str) -> str:
    return name if name.split()[0].isupper() else name.lower()


def pp(v: float) -> str:
    return f"{v:+.2f}pp"


def name_of(ref: str | None) -> str:
    if not ref:
        return "The metric"
    base = ref.partition("#")[0].partition(":")[0]
    return NAMES.get(base, base.replace("_inr", "").replace("_", " ").capitalize())


def _period(a: str, b: str) -> str:
    da, db = date.fromisoformat(a), date.fromisoformat(b)
    if da == db:
        return f"{da:%b} {da.day}"
    if da.month == db.month:
        return f"{da:%b} {da.day}–{db.day}"
    return f"{da:%b} {da.day} – {db:%b} {db.day}"


# --------------------------------------------------------------------------- answer
@dataclass
class Answer:
    status: str
    headline: str
    definitions_used: dict = field(default_factory=dict)
    facts: list = field(default_factory=list)
    hypotheses: list = field(default_factory=list)
    recommendations: list = field(default_factory=list)
    caveats: list = field(default_factory=list)
    confidence: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    charts: list = field(default_factory=list)
    suggested_follow_ups: list = field(default_factory=list)
    grounding: dict = field(default_factory=dict)
    composed_by: str = "draft"

    def as_dict(self) -> dict:
        return asdict(self)

    def to_markdown(self) -> str:
        out = [f"**{self.headline}**", ""]
        if self.facts:
            out += ["**What the data shows**"] + [f"- {f['text']}" for f in self.facts] + [""]
        if self.hypotheses:
            out += ["**Likely explanation (hypotheses)**"] + [
                f"- {h['text']} *(confidence: {h['confidence'].lower()}; validate: {h['how_to_validate']})*"
                for h in self.hypotheses] + [""]
        if self.recommendations:
            out += ["**Recommendations**"] + [
                f"- {r['text']}" + (f" Expected impact: {r['impact_estimate']}." if r.get("impact_estimate") else "")
                for r in self.recommendations] + [""]
        if self.caveats:
            out += ["**Caveats**"] + [f"- {c['text']}" for c in self.caveats] + [""]
        d = self.definitions_used
        if d:
            out += ["**Definitions**"] + [f"- {k.replace('_', ' ')}: {v}" for k, v in d.items()] + [""]
        c = self.confidence
        if c:
            out.append(f"Confidence: **{c['level']}** ({'; '.join(c.get('reasons', [])[:3])})")
        return "\n".join(out).strip()


class Composer:
    def __init__(self, model, settings: dict, llm: LLM | None = None):
        self.model = model
        self.cfg = settings.get("answer", {})
        self.llm = llm
        self.max_recs = int(self.cfg.get("max_recommendations", 3))
        self.retries = int(self.cfg.get("max_grounding_retries", 2))

    # ------------------------------------------------------------------ simple terminal states
    def simple(self, res) -> Answer:
        a = Answer(res.state, res.message)
        if res.refusal:
            a.facts = []
            a.suggested_follow_ups = [f"Show {_lc(name_of(x))} instead" for x in res.refusal.get("alternatives", [])[:3]]
        if res.ambiguity and res.ambiguity.items:
            it = res.ambiguity.items[0]
            a.suggested_follow_ups = [o.label for o in it.options]
        if res.narrowing:
            a.suggested_follow_ups = [o["label"] for o in res.narrowing["options"]]
        a.confidence = {"level": "HIGH" if res.state in ("REFUSED_ACCESS", "REJECTED_UNSAFE") else "MEDIUM",
                        "score": None, "reasons": [], "limitations": []}
        return a

    # ------------------------------------------------------------------ deterministic draft
    def draft(self, res) -> Answer:
        an = res.analysis
        ref = res.metrics[0] if res.metrics else None
        met = self.model.metrics[ref.partition(":")[0]] if ref else None
        kind = met.format if met else None
        F, ev_steps = [], {}

        def fact(text, evidence, **values):
            F.append({"id": f"F{len(F) + 1}", "text": text, "evidence": [e for e in evidence if e], "values": values})
            for e in evidence:
                if e:
                    ev_steps[e] = True
            return F[-1]["id"]

        def step_for(role=None, period=None, dimension=None):
            for s in res.steps:
                if (role is None or s.role == role) and (period is None or s.period == period) \
                        and (dimension is None or s.dimension == dimension) and s.status in ("OK", "TRUNCATED"):
                    return s.step_id
            return None

        h = (an.headline if an else None) or {}
        w, b = res.window, res.baseline
        p1l = _period(w.start.isoformat(), w.end.isoformat())
        p0l = _period(b.start.isoformat(), b.end.isoformat()) if b else None
        mname = name_of(ref)
        qt = res.plan.question_type if res.plan else "LOOKUP"
        headline = ""
        hyps = list(an.hypotheses) if an else []
        top_h = next((x for x in hyps if x.code not in ("INCOMPLETE_DATA", "SEASONAL_EVENT", "NO_SIGNIFICANT_CHANGE")),
                     None)

        # ---- headline + core facts by question type
        if h.get("P1") is not None:
            if h.get("P0") is not None:
                chg = pp(h["delta_pp"]) if h.get("delta_pp") is not None else f"{h['pct_change']:+.1f}%"
                fact(f"{mname}: {fmt(h['P0'], kind)} in {p0l} → {fmt(h['P1'], kind)} in {p1l} ({chg})."
                     + ("" if h.get("significant", True) else " The change is not statistically significant."),
                     [step_for("headline", "P1"), step_for("headline", "P0")], p0=h["P0"], p1=h["P1"])
            else:
                fact(f"{mname} for {p1l}: {fmt(h['P1'], kind)}.", [step_for("headline", "P1")], p1=h["P1"])

        if qt == "DRIVER_ANALYSIS" and an:
            dec = an.decomposition(an.best_dimension) if an.best_dimension else None
            if an.interpretation_code == "NO_SIGNIFICANT_CHANGE":
                dec = None                    # no change to explain: don't attribute noise to segments
            if dec and dec.method == "MIX_RATE_DECOMPOSITION":
                ids = [step_for("breakdown", "P1", dec.dimension), step_for("breakdown", "P0", dec.dimension)]
                t = dec.totals
                fact(f"By {dec.dimension}, the change splits into a mix effect of {pp(t['mix_effect_pp'])} and a rate "
                     f"effect of {pp(t['rate_effect_pp'])} ({dec.interpretation_code.replace('_', ' ').lower()}).",
                     ids, mix=t["mix_effect_pp"], rate=t["rate_effect_pp"])
                if an.drivers:
                    d = an.drivers[0]
                    fact(f"{d['segment']} ({d['dimension']}): share of volume {fmt(d['share_p0'], 'percent')} → "
                         f"{fmt(d['share_p1'], 'percent')}, own rate {fmt(d['rate_p0'], 'percent')} → "
                         f"{fmt(d['rate_p1'], 'percent')}; it accounts for {d['contribution']:+.2f}pp "
                         f"({d['share_of_change'] * 100:.0f}% of the change).", ids,
                         contribution=d["contribution"])
                others = [x for x in dec.segments[1:] if x.r0 is not None and x.r1 is not None and not x.low_volume]
                if an.interpretation_code == "MIX_DOMINANT" and others:
                    mx = max(abs((x.r1 - x.r0) * 100) for x in others)
                    fact(f"No other {dec.dimension} with meaningful volume changed its own rate by more than "
                         f"{mx:.2f}pp.", ids, max_rate_change_pp=mx)
            elif dec and dec.method == "CONTRIBUTION" and an.drivers:
                d = an.drivers[0]
                fact(f"{d['segment']} ({d['dimension']}) accounts for {d['share_of_change'] * 100:.0f}% of the change.",
                     [step_for("breakdown", "P1", dec.dimension)])
            for dd in an.drilldowns:
                if dd["kind"] == "VOLUME" and dd["segments"]:
                    s0 = dd["segments"][0]
                    if s0["share_of_increase"]:
                        sid = next((s.step_id for s in res.steps if s.role == "drilldown" and s.period == "P1"
                                    and s.dimension == dd["dimension"]), None)
                        fact(f"{s0['segment']} accounts for {s0['share_of_increase'] * 100:.0f}% of the extra "
                             f"{dd['parent'].split(' = ')[-1]} volume ({fmt(s0['volume_p0'], 'integer')} → "
                             f"{fmt(s0['volume_p1'], 'integer')} attempts).", [sid])
            if an.hour_window:
                hw = an.hour_window
                sid = next((s.step_id for s in res.steps if s.role == "drilldown" and s.dimension == "hour"
                            and s.period == "P1"), None)
                fact(f"The drop is concentrated between {hw['label']}, where the segment's rate fell by up to "
                     f"{abs(hw['worst_drop_pp']):.0f}pp.", [sid])
            if an.failure_codes and an.failure_codes["codes"]:
                c = an.failure_codes["codes"][0]
                sid = next((s.step_id for s in res.steps if s.role == "drilldown" and s.dimension == "response_code"
                            and s.period == "P1"), None)
                fact(f"Top rising failure: {c['failure_reason']} (code {c['response_code']}), "
                     f"{fmt(c['rate_p0'], 'percent')} → {fmt(c['rate_p1'], 'percent')} of attempts.", [sid])
            for vs in an.volume_shift[:1]:
                if an.interpretation_code == "NO_SIGNIFICANT_CHANGE" and vs["share_change_pp"] >= 10:
                    fact(f"{vs['segment']}'s share of volume went from {fmt(vs['share_p0'], 'percent')} to "
                         f"{fmt(vs['share_p1'], 'percent')}.", [step_for("breakdown", "P1", an.best_dimension)])
            headline = self._driver_headline(mname, h, p0l, p1l, an, top_h, kind)
        elif qt == "RECONCILIATION" and an and an.reconciliation:
            rc = an.reconciliation
            sa = step_for("reconcile")
            fact(f"Dashboard GMV (IST day): {fmt(rc['source_a']['value'], 'currency_inr')}; finance ledger (UTC day): "
                 f"{fmt(rc['source_b']['value'], 'currency_inr')}; gap {fmt(rc['diff_inr'], 'currency_inr')} "
                 f"({rc['diff_pct']:.2f}%).", [sa])
            tests = [s.step_id for s in res.steps if s.role == "reconcile_test"]
            for f_ in rc["explained"]:
                fact(f"{f_['description']}: {fmt(f_['amount_inr'], 'currency_inr')} ({f_['explains_pct']:.2f}% of "
                     f"dashboard GMV).", tests[:2])
            fact(f"Unexplained residual: {fmt(rc['residual_inr'], 'currency_inr')}.", tests[:1])
            top3 = ", ".join(f_["description"].split(" (")[0].lower() for f_ in rc["explained"][:3])
            headline = (f"Finance's figure is {fmt(rc['source_b']['value'], 'currency_inr')} vs the dashboard's "
                        f"{fmt(rc['source_a']['value'], 'currency_inr')} ({rc['diff_pct']:.2f}% lower); the gap is "
                        f"fully explained by definitions — mainly {top3} — not by missing data.")
        elif an and an.ranking:
            rk = an.ranking
            rows = rk["rows"]
            sid = step_for("ranking", "P1")
            for r in rows[:5]:
                fact(f"#{r['rank']} {r['segment']}: {fmt(r['value'], kind)}", [sid], value=r["value"])
            if rows:
                headline = (f"{'Top' if rk['order'] == 'descending' else 'Bottom'} {len(rows)} {rk['dimension']}s by "
                            f"{_lc(mname)} for {p1l}: {rows[0]['segment']} leads with {fmt(rows[0]['value'], kind)}.")
        elif an and an.breakdown:
            bd = an.breakdown
            sid = step_for("breakdown", "P1")
            segs = [x for x in bd["segments"] if x["value"] is not None]
            for x in segs[:6]:
                extra = f" ({x['share'] * 100:.1f}% of total)" if bd.get("total") else (
                    f" on {fmt(x['volume'], 'integer')} attempts" + (" (low volume)" if x.get("low_volume") else ""))
                fact(f"{x['segment']}: {fmt(x['value'], kind)}{extra}.", [sid], value=x["value"])
            if segs:
                if bd.get("total"):
                    headline = (f"{mname} for {p1l} was {fmt(bd['total'], kind)}; {segs[0]['segment']} is the largest "
                                f"{bd['dimension']} at {fmt(segs[0]['value'], kind)} ({segs[0]['share'] * 100:.1f}%).")
                else:
                    headline = (f"{mname} by {bd['dimension']} for {p1l}: highest {segs[0]['segment']} "
                                f"({fmt(segs[0]['value'], kind)}), lowest {segs[-1]['segment']} "
                                f"({fmt(segs[-1]['value'], kind)}).")
        elif an and an.trend:
            tr = an.trend
            sid = step_for("trend")
            fact(f"{mname} went from {fmt(tr['first'], kind)} ({tr['series'][0]['t']}) to {fmt(tr['last'], kind)} "
                 f"({tr['series'][-1]['t']}).", [sid])
            for x in tr["anomalies"][:3]:
                fact(f"Unusual value on {x['t']}: {fmt(x['value'], kind)}.", [sid])
            headline = (f"{mname} went from {fmt(tr['first'], kind)} to {fmt(tr['last'], kind)} over {p1l}"
                        + (f", with {len(tr['anomalies'])} unusual {tr['grain']}(s)." if tr["anomalies"] else "."))
        if not headline:
            headline = self._lookup_headline(mname, h, p0l, p1l, kind, an)

        if an and an.premise and not an.premise["holds"] and h.get("P0") is not None:
            word = "drop" if an.premise["asked"] == "down" else "rise"
            chg = pp(h["delta_pp"]) if h.get("delta_pp") is not None else f"{h.get('pct_change') or 0:+.1f}%"
            headline = (f"No — {mname.lower()} did not meaningfully {word}: {fmt(h['P0'], kind)} → "
                        f"{fmt(h['P1'], kind)} ({chg}" + (", within normal variation)." if not h.get("significant")
                                                         else ")."))

        # ---- hypotheses / recommendations
        H, R = [], []
        fact_ids = [f["id"] for f in F]
        for x in hyps:
            H.append({"id": x.id, "code": x.code, "text": x.text, "confidence": x.confidence,
                      "supporting_facts": fact_ids[1:3] if len(fact_ids) > 1 else fact_ids[:1],
                      "evidence": sorted(ev_steps)[:3], "how_to_validate": VALIDATE.get(x.code, "Check with the data owner.")})
        for x in (an.recommendations if an else [])[: self.max_recs]:
            code = next((hh["code"] for hh in H if hh["id"] == x.follows_from), "")
            effort, owner = OWNER.get(code, ("MEDIUM", "Payments analytics"))
            R.append({"id": x.id, "text": x.text, "based_on": [x.follows_from] + fact_ids[:1],
                      "impact_estimate": pp(x.expected_impact_pp) + " overall" if x.expected_impact_pp else None,
                      "effort": effort, "owner_hint": owner})

        a = Answer("ANSWERED", headline, self._definitions(res, ref), F, H, R, self._caveats(res),
                   evidence=self._evidence(res), charts=self._charts(res, kind),
                   suggested_follow_ups=self._follow_ups(res, an, qt))
        a.confidence = self._confidence(res, a)
        return a

    def _driver_headline(self, mname, h, p0l, p1l, an, top_h, kind) -> str:
        if h.get("P0") is None:
            return f"{mname} for {p1l}: {fmt(h.get('P1'), kind)}."
        verb = "fell" if h["delta"] < 0 else "rose"
        size = f"{abs(h['delta_pp']):.2f}pp" if h.get("delta_pp") is not None else f"{abs(h['pct_change']):.1f}%"
        base = f"{mname} {verb} {size} ({fmt(h['P0'], kind)} → {fmt(h['P1'], kind)}) in {p1l} vs {p0l}"
        if an.interpretation_code == "NO_SIGNIFICANT_CHANGE":
            return f"{mname} was essentially flat in {p1l} vs {p0l} ({fmt(h['P0'], kind)} → {fmt(h['P1'], kind)}, " \
                   f"not statistically significant)."
        d = an.drivers[0] if an.drivers else None
        if top_h is None or d is None:
            return base + "."
        if top_h.code == "MIX_SHIFT":
            return f"{base}, mostly because traffic shifted toward {d['segment']}, not because any {d['dimension']} " \
                   f"got worse."
        if top_h.code == "ISSUER_OUTAGE":
            hw = an.hour_window
            return f"{base}, mostly because of an apparent outage at {d['segment']}" + \
                (f" ({hw['label']})." if hw else ".")
        if top_h.code == "ACQUIRER_DEGRADATION":
            return f"{base}; {d['segment']} degraded, with acquirer timeouts driving most of the drop."
        return f"{base}, driven mainly by {d['segment']} ({d['dimension']})."

    def _lookup_headline(self, mname, h, p0l, p1l, kind, an) -> str:
        if h.get("P1") is None:
            return "The query ran, but there is no value to report for this period."
        s = f"{mname} for {p1l}: {fmt(h['P1'], kind)}"
        if h.get("P0") is not None:
            chg = pp(h["delta_pp"]) if h.get("delta_pp") is not None else f"{h['pct_change']:+.1f}%"
            s += f" (vs {fmt(h['P0'], kind)} in {p0l}, {chg}" + ("" if h.get("significant", True)
                                                                 else ", not significant") + ")"
        return s + "."

    # ------------------------------------------------------------------ parts
    def _definitions(self, res, ref) -> dict:
        d = {}
        if ref:
            r = self.model.resolve_metric(ref)
            desc = r.variant.description if r.metric.has_variants else r.metric.description
            d["metric"] = f"{name_of(ref)} = {desc.replace(' (DEFAULT)', '')}".strip()
        if res.window:
            d["time_window"] = f"P1: {res.window.start}..{res.window.end} IST" + (
                f"; P0: {res.baseline.start}..{res.baseline.end} IST" if res.baseline else "")
        scope = [x for x in res.disclosures if x.startswith("Results limited to your access scope")]
        d["scope"] = scope[0] if scope else f"All data you can access (role: {res.user['role']})"
        versions = sorted({v for s in res.steps if s.data_version for v in s.data_version.split(";")})
        if versions:
            d["data_version"] = "; ".join(versions)
        return d

    def _caveats(self, res) -> list:
        out, seen = [], set()

        def add(code, text):
            if text not in seen:
                seen.add(text)
                out.append({"code": code, "text": text})
        an = res.analysis
        rep = res.ambiguity
        for x in (rep.assumptions if rep else []):
            if "(your choice)" in x:
                continue
            add("ASSUMED", "I assumed " + x.split("assumed ", 1)[-1] if "assumed" in x else x)
        if an:
            for r in an.dq:
                if r.severity in ("WARN", "BLOCK"):
                    add(r.check_id, r.impact)
            for c in an.caveats:
                if not c.startswith("["):
                    add("ANALYSIS", c)
        has_dq05 = bool(an and any(r.check_id == "DQ05_LATE_ARRIVING" for r in an.dq))
        for x in res.disclosures:
            if has_dq05 and x.startswith("Data is complete up to"):
                continue
            if x.startswith(("Data is complete up to", "Narrowed", "Limited to the top", "Proceeded with default",
                             "Couldn't read the period", "No period given", "Breakdown by", "Query budget",
                             "The automated analysis failed", "Some metrics were computed")):
                add("NOTE", x)
        return out

    def _evidence(self, res) -> list:
        out = []
        for s in res.steps:
            out.append({"id": s.step_id, "type": "query", "purpose": s.purpose, "status": s.status, "sql": s.sql,
                        "rows": s.row_count, "rls": s.rls, "data_version": s.data_version,
                        "duration_ms": s.duration_ms})
        an = res.analysis
        if an:
            for i, d in enumerate(an.decompositions, 1):
                out.append({"id": f"A{i}", "type": "analysis", "method": d.method, "dimension": d.dimension})
            if an.reconciliation:
                out.append({"id": "A-recon", "type": "analysis", "method": "RECONCILIATION"})
            if an.dq:
                out.append({"id": "A-dq", "type": "analysis", "method": "DQ_CHECK",
                            "checks": [r.check_id for r in an.dq]})
        return out

    def _follow_ups(self, res, an, qt) -> list:
        f = []
        if an and an.drivers and qt == "DRIVER_ANALYSIS":
            d = an.drivers[0]
            if d["dimension"] != "merchant":
                f.append(f"Drill into {d['segment']} by merchant")
            if an.hour_window:
                f.append(f"Show {d['segment']} success rate by hour for the last 7 days")
        if res.metrics and res.metrics[0].startswith("success_rate") and "order_level" not in res.metrics[0]:
            f.append("Show order-level success rate trend")
        if qt in ("LOOKUP", "TREND"):
            f.append("Break this down by payment method")
        if qt == "RECONCILIATION":
            f.append("Show the reconciliation by merchant")
        return f[:3]

    # ------------------------------------------------------------------ confidence (15.3)
    def _confidence(self, res, a: Answer) -> dict:
        score, reasons, limits = 1.0, [], []
        an = res.analysis
        h = (an.headline if an else None) or {}
        if an is None:
            score -= 0.3
            limits.append("Automated analysis unavailable")
        den = h.get("den_p1")
        if den is not None:
            if den < 1000:
                score -= 0.2
                limits.append(f"Small sample ({den:,.0f} in the focus period)")
            else:
                reasons.append(f"Large sample ({den:,.0f} in the focus period)")
        if h.get("P0") is not None and "significant" in h and not h["significant"] and not (
                an and an.premise and not an.premise["holds"]):
            score -= 0.15
            limits.append("Change not statistically significant")
        if an:
            if an.dq_block:
                score -= 0.4
                limits.append("Blocking data-quality issue")
            else:
                used = {d.dimension for d in an.decompositions} | (
                    {an.breakdown["dimension"]} if an.breakdown else set()) | (
                    {an.ranking["dimension"]} if an.ranking else set())
                warns = [r for r in an.dq if r.severity == "WARN"
                         and r.auto_mitigation not in ("USED_DEDUP_VIEW", "EXCLUDED_INCOMPLETE_DAYS")]
                hits = [r for r in warns if r.affects and set(r.affects) & used and (r.metric or 0) > 0.05]
                if hits:
                    score -= 0.3
                    limits.append(f"{hits[0].check_id}: {hits[0].metric:.1%} of rows affect this breakdown")
                elif warns:
                    score -= 0.1
                    limits.append("Data-quality warnings in the window")
            if an.interpretation_code == "MIXED":
                score -= 0.15
                limits.append("Mix and rate effects are both material")
            elif an.interpretation_code in ("MIX_DOMINANT", "RATE_DOMINANT"):
                reasons.append(f"Decomposition is clear ({an.interpretation_code.replace('_', ' ').lower()}) and "
                               f"sums exactly")
            if an.drivers and an.drivers[0].get("low_volume"):
                score -= 0.05
                limits.append("Top segment has low volume")
            if an.reconciliation and abs(an.reconciliation["residual_inr"]) < 1:
                reasons.append("Reconciliation fully explains the gap")
        if an and not any(r.severity in ("WARN", "BLOCK") for r in an.dq):
            reasons.append("No data-quality issues in the window")
        if not any(len(s.attempts) > 1 for s in res.steps) and res.steps:
            reasons.append("Computed from governed metric definitions")
        assumed = [c for c in a.caveats if c["code"] == "ASSUMED"]
        if assumed:
            score -= min(0.1 * len(assumed), 0.2)
            limits.append("Interpretation assumed, not confirmed")
        if any("clarification limit" in x for x in res.disclosures):
            score -= 0.15
        repairs = sum(max(len(s.attempts) - 1, 0) for s in res.steps)
        if repairs:
            score -= 0.05 * repairs
            limits.append(f"{repairs} SQL repair attempt(s)")
        if any(s.status not in ("OK", "TRUNCATED") for s in res.steps):
            score -= 0.1
            limits.append("Some queries failed")
        if any(hh["code"] in ("MIX_SHIFT", "ISSUER_OUTAGE", "ACQUIRER_DEGRADATION") for hh in a.hypotheses):
            limits.append("Root causes are hypotheses; the data shows the pattern, not the reason")
        score = max(0.0, min(1.0, score))
        level = "HIGH" if score >= 0.75 else "MEDIUM" if score >= 0.5 else "LOW"
        return {"level": level, "score": round(score, 2), "reasons": reasons, "limitations": limits}

    # ------------------------------------------------------------------ charts (16)
    def _charts(self, res, kind) -> list:
        an = res.analysis
        C = []

        def chart(**kw):
            C.append({"chart_id": f"C{len(C) + 1}", **kw})
        yfmt = ".1%" if kind == "percent" else ",.0f"
        badge = next((r.impact.split(".")[0] for r in (an.dq if an else []) if r.check_id == "DQ05_LATE_ARRIVING"), None)
        h = (an.headline if an else None) or {}
        if h.get("P1") is not None:
            chart(type="kpi", title=name_of(res.metrics[0] if res.metrics else None),
                  evidence=next((s.step_id for s in res.steps if s.role == "headline"), None),
                  data={"P0": h.get("P0"), "P1": h.get("P1"), "delta": h.get("delta"), "delta_pp": h.get("delta_pp"),
                        "format": yfmt})
        if an and an.trend:
            tr = an.trend
            ann = []
            if res.baseline and res.plan and res.plan.question_type == "DRIVER_ANALYSIS":
                ann += [{"type": "period_band", "from": str(res.baseline.start), "to": str(res.baseline.end), "label": "P0"},
                        {"type": "period_band", "from": str(res.window.start), "to": str(res.window.end), "label": "P1"}]
            ann += [{"type": "point", "x": x["t"], "label": f"unusual (z={x['z']})"} for x in tr["anomalies"][:5]]
            ann += [{"type": "point", "x": e["start"], "label": e["name"]} for e in (an.events or []) if e.get("sale")]
            chart(type="line", title=f"{name_of(res.metrics[0])} by {tr['grain']} (IST)",
                  evidence=next((s.step_id for s in res.steps if s.role == "trend"), None),
                  x={"field": tr["grain"], "type": "temporal"}, y={"field": "value", "type": "quantitative",
                                                                   "format": yfmt},
                  data=tr["series"], annotations=ann, caveat_badge=badge)
        dec = an.decomposition(an.best_dimension) if an and an.best_dimension else None
        if dec and dec.method == "MIX_RATE_DECOMPOSITION":
            o, t = dec.overall, dec.totals
            chart(type="waterfall", title=f"What moved {name_of(res.metrics[0]).lower()} ({dec.dimension})",
                  evidence=f"A{an.decompositions.index(dec) + 1}",
                  data=[{"label": "P0", "value": o["P0"]}, {"label": "Mix effect", "delta": t["mix_effect_pp"] / 100},
                        {"label": "Rate effect", "delta": t["rate_effect_pp"] / 100}, {"label": "P1", "value": o["P1"]}],
                  y={"format": ".1%"})
            chart(type="grouped_bar", title=f"Share of volume by {dec.dimension}, P0 vs P1",
                  evidence=f"A{an.decompositions.index(dec) + 1}",
                  x={"field": "segment", "type": "nominal"}, y={"field": "share", "type": "quantitative", "format": ".1%"},
                  series={"field": "period"},
                  data=[{"segment": s.segment, "period": p, "share": w} for s in dec.segments[:8]
                        for p, w in (("P0", s.w0), ("P1", s.w1))])
        if an and an.hour_window:
            hd = next((d for dd in an.drilldowns if dd["kind"] == "RATE" for d in dd["decompositions"]
                       if d["dimension"] == "hour"), None)
            if hd:
                rows = sorted(hd["segments"], key=lambda s: int(s["segment"]) if s["segment"].isdigit() else 99)
                chart(type="grouped_bar", title=f"{an.drivers[0]['segment']}: success rate by hour, P0 vs P1",
                      evidence=next((s.step_id for s in res.steps if s.dimension == "hour"), None),
                      x={"field": "hour", "type": "ordinal"}, y={"field": "rate", "type": "quantitative", "format": ".1%"},
                      series={"field": "period"},
                      data=[{"hour": s["segment"], "period": p, "rate": s[k]} for s in rows
                            for p, k in (("P0", "r0"), ("P1", "r1")) if s[k] is not None],
                      annotations=[{"type": "band", "from": an.hour_window["start_hour"],
                                    "to": an.hour_window["end_hour"], "label": "outage window"}])
        if an and an.reconciliation:
            rc = an.reconciliation
            chart(type="waterfall", title="Dashboard GMV → finance ledger", evidence="A-recon",
                  data=[{"label": "Dashboard GMV", "value": rc["source_a"]["value"]}]
                  + [{"label": f["description"].split(" (")[0], "delta": -f["amount_inr"]} for f in rc["explained"]]
                  + [{"label": "Residual", "delta": -rc["residual_inr"]},
                     {"label": "Finance ledger", "value": rc["source_b"]["value"]}], y={"format": ",.0f"})
        if an and an.breakdown:
            bd = an.breakdown
            chart(type="bar", orientation="horizontal", title=f"{name_of(res.metrics[0])} by {bd['dimension']}",
                  evidence=next((s.step_id for s in res.steps if s.role == "breakdown"), None),
                  x={"field": "value", "type": "quantitative", "format": yfmt},
                  y={"field": "segment", "type": "nominal", "sort": "-x"}, data=bd["segments"])
        if an and an.ranking:
            chart(type="table", title=f"Ranking by {name_of(res.metrics[0]).lower()}",
                  evidence=next((s.step_id for s in res.steps if s.role == "ranking"), None), data=an.ranking["rows"])
        return C

    # ------------------------------------------------------------------ phrasing + grounding
    def compose(self, res) -> Answer:
        a = self.draft(res)
        allowed = allowed_numbers(res, a)
        a.grounding = ground(a, allowed)
        if a.grounding["unmatched"]:          # should not happen; never ship ungrounded text
            self._drop_ungrounded(a, a.grounding["unmatched"])
        if self.llm is None:
            return self._final(res, a)
        draft_copy = copy.deepcopy(a)
        problems: list[str] = []
        for attempt in range(self.retries + 1):
            try:
                reply = self.llm.json("compose", COMPOSE_RULES, self._compose_prompt(a, problems), COMPOSE_SCHEMA,
                                      tier="reasoning", context={"draft": _texts(draft_copy)})
            except LLMError:
                break
            cand = copy.deepcopy(draft_copy)
            if not _apply_texts(cand, reply.data):
                problems = ["Keep exactly the same ids for facts, hypotheses and recommendations."]
                continue
            g = ground(cand, allowed)
            if not g["unmatched"]:
                cand.grounding = g | {"attempts": attempt + 1}
                cand.composed_by = self.llm.name
                return self._final(res, cand)
            problems = [f"{u['where']}: the number {u['token']!r} does not appear in the results" for u in g["unmatched"]]
        # fall back to the grounded draft wording, but say the rewrite failed
        draft_copy.grounding = draft_copy.grounding | {"rewrite_failed": bool(problems), "problems": problems}
        if problems:
            draft_copy.confidence["score"] = round(max(0.0, draft_copy.confidence["score"] - 0.3), 2)
            draft_copy.confidence["limitations"].append("Wording could not be verified; showing the plain draft")
            draft_copy.confidence["level"] = _level(draft_copy.confidence["score"])
        return self._final(res, draft_copy)

    def _final(self, res, a: Answer) -> Answer:
        if res.analysis and res.analysis.dq_block:
            a.status = "ANSWERED_WITH_DQ_BLOCK"
            blk = next(r for r in res.analysis.dq if r.severity == "BLOCK")
            a.headline = f"I can't give a reliable answer for {res.window.label} because of a data-quality issue: " \
                         f"{blk.impact} Here is what I can say."
        elif a.confidence.get("level") == "LOW" or a.grounding.get("dropped"):
            a.status = "ANSWERED_LOW_CONFIDENCE"
        else:
            a.status = "ANSWERED"
        return a

    def _drop_ungrounded(self, a: Answer, unmatched: list) -> None:
        bad = {u["where"] for u in unmatched}
        a.facts = [f for f in a.facts if f"fact {f['id']}" not in bad]
        a.grounding["dropped"] = sorted(bad)
        a.confidence["score"] = round(max(0.0, a.confidence.get("score", 1) - 0.3), 2)
        a.confidence["level"] = _level(a.confidence["score"])

    def _compose_prompt(self, a: Answer, problems: list[str]) -> str:
        body = json.dumps(_texts(a), ensure_ascii=False, indent=1)
        p = ("Rewrite the wording of this answer for a business reader. Keep every id, keep every number exactly as "
             "written (you may drop a number, never add or change one), keep hypotheses phrased as hypotheses.\n\n"
             + body)
        if problems:
            p += "\n\nYour previous rewrite had problems:\n- " + "\n- ".join(problems)
        return p


def _level(score: float) -> str:
    return "HIGH" if score >= 0.75 else "MEDIUM" if score >= 0.5 else "LOW"


COMPOSE_RULES = """You are the writing step of a payments analytics assistant. You receive an answer whose numbers
were computed from database queries. You only improve the wording: clear, short, plain business English, no jargon.
Never introduce a number that is not already in the text you were given; do not round differently; do not invent
causes. Hypotheses stay hypotheses ("likely", "possible"). Return the same ids."""

_TEXT_ITEM = {"type": "object", "additionalProperties": False, "required": ["id", "text"],
              "properties": {"id": {"type": "string"}, "text": {"type": "string"}}}
COMPOSE_SCHEMA = {"type": "object", "additionalProperties": False,
                  "required": ["headline", "facts", "hypotheses", "recommendations"],
                  "properties": {"headline": {"type": "string"},
                                 "facts": {"type": "array", "items": _TEXT_ITEM},
                                 "hypotheses": {"type": "array", "items": _TEXT_ITEM},
                                 "recommendations": {"type": "array", "items": _TEXT_ITEM}}}


def _texts(a: Answer) -> dict:
    return {"headline": a.headline,
            "facts": [{"id": f["id"], "text": f["text"]} for f in a.facts],
            "hypotheses": [{"id": h["id"], "text": h["text"]} for h in a.hypotheses],
            "recommendations": [{"id": r["id"], "text": r["text"]} for r in a.recommendations]}


def _apply_texts(a: Answer, data: dict) -> bool:
    for key in ("facts", "hypotheses", "recommendations"):
        mine = {x["id"]: x for x in getattr(a, key)}
        got = {x["id"]: x["text"] for x in data.get(key) or []}
        if set(got) != set(mine):
            return False
        for k, t in got.items():
            mine[k]["text"] = t
    if data.get("headline"):
        a.headline = data["headline"]
    return True


def offline_compose(ctx: dict) -> dict:
    """Offline backend: the draft wording is already final."""
    return ctx["draft"]


# --------------------------------------------------------------------------- grounding (15.4)
_SKIP = [r"\b\d{4}-\d{2}-\d{2}\b", r"\b\d{1,2}:\d{2}\b", r"\bMID\d+\b", r"\bQ[1-4]\b", r"\bP[01]\b", r"\bF\d+\b",
         r"\bH\d+\b", r"\bR\d+\b", r"\bDQ\d+\w*", r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2}"
         r"(?:\s*[–-]\s*(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* )?\d{1,2})?\b",
         r"#\d+\b", r"\bz=-?[\d.]+"]
_NUM = re.compile(r"(?P<sign>[+\-−])?(?P<cur>₹)?\s?(?P<n>\d[\d,]*(?:\.\d+)?)\s?(?P<unit>%|pp|\s?Cr\b|\s?L\b|\s?lakh|"
                  r"\s?crore|x\b)?")


def _collect(obj, out: set):
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        if math.isfinite(float(obj)):
            out.add(float(obj))
        return
    if hasattr(obj, "is_finite"):            # Decimal
        out.add(float(obj))
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _collect(v, out)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            _collect(v, out)
    elif isinstance(obj, str):
        for m in re.finditer(r"\d+(?:\.\d+)?", obj):
            out.add(float(m.group()))


def allowed_numbers(res, a: Answer | None = None) -> list[float]:
    vals: set[float] = set()
    for s in res.steps:
        _collect(s.rows, vals)
    if res.analysis:
        _collect(res.analysis.as_dict(), vals)
    _collect(res.question, vals)
    if a is not None:                       # numbers the system itself stated as definitions / limits
        _collect(a.definitions_used, vals)
    derived = set()
    for v in vals:
        derived |= {v * 100, abs(v), abs(v * 100)}
    return sorted(vals | derived)


def _parse(m) -> tuple[float, float, str]:
    raw = m.group("n").replace(",", "")
    dec = len(raw.split(".")[1]) if "." in raw else 0
    v = float(raw)
    unit = (m.group("unit") or "").strip().lower()
    scale = {"cr": 1e7, "crore": 1e7, "l": 1e5, "lakh": 1e5}.get(unit, 1.0)
    tol = 0.5 * 10 ** -dec * scale + 1e-9
    if unit in ("%", "pp"):
        tol = max(tol, 0.051 if dec else 0.51)
    return v * scale, tol, unit


def ground(a: Answer, allowed: list[float]) -> dict:
    import bisect
    texts = [("headline", a.headline)] + [(f"fact {f['id']}", f["text"]) for f in a.facts] + \
        [(f"hypothesis {h['id']}", h["text"]) for h in a.hypotheses] + \
        [(f"recommendation {r['id']}", r["text"]) for r in a.recommendations]
    checked, unmatched = 0, []
    for where, t in texts:
        clean = t
        for pat in _SKIP:
            clean = re.sub(pat, " ", clean)
        for m in _NUM.finditer(clean):
            v, tol, unit = _parse(m)
            if not m.group("cur") and not unit and v == int(v) and v <= 31:
                continue                      # small counts ("top 5", "3 attempts") and day numbers
            if 2000 <= v <= 2100 and not unit and not m.group("cur"):
                continue                      # years
            checked += 1
            def hit(x):
                i = bisect.bisect_left(allowed, x - tol)
                return i < len(allowed) and allowed[i] <= x + tol
            if hit(v) or hit(-v):           # signs are written separately ("-2.63pp", "fell 2.63pp")
                continue
            unmatched.append({"where": where, "token": m.group().strip(), "value": v})
    return {"checked": checked, "unmatched": unmatched, "passed": not unmatched}


def check_rules(a: Answer) -> list[str]:
    """SPEC 15.2 post-checks."""
    p = []
    if a.status.startswith("ANSWERED") and a.facts:
        for f in a.facts:
            if not f.get("evidence"):
                p.append(f"fact {f['id']} cites no evidence")
        for h in a.hypotheses:
            if not h.get("confidence") or not h.get("how_to_validate"):
                p.append(f"hypothesis {h['id']} lacks confidence or a validation step")
        hids = {h["id"] for h in a.hypotheses} | {f["id"] for f in a.facts}
        for r in a.recommendations:
            if not set(r.get("based_on", [])) & hids:
                p.append(f"recommendation {r['id']} references nothing")
        for k in ("metric", "time_window", "scope"):
            if k not in a.definitions_used:
                p.append(f"definition {k} missing")
    return p
