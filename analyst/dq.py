"""Data-quality checks over the window and tables a plan touches (SPEC section 13).

These are fixed, parameterised system queries (not user-derived SQL), run on the
executor's sandboxed read-only connection. They report rates and dates, never
row values, so they are safe to show to any role. Results are cached per
(check, window, data_version).

Severity: INFO < WARN < BLOCK. A BLOCK applies only to the analyses it affects
(e.g. merchant breakdowns when merchant keys are missing), not to everything.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from datetime import date, timedelta

SEVERITY = {"OK": 0, "INFO": 1, "WARN": 2, "BLOCK": 3}
MERCHANT_DIMS = {"merchant", "merchant_id", "merchant_segment", "merchant_segment_current", "category", "region",
                 "city", "city_tier", "integration_type", "account_manager", "risk_band"}


@dataclass
class DQResult:
    check_id: str
    table: str
    window: list[str]
    severity: str
    metric: float | None
    threshold: float | None
    impact: str
    auto_mitigation: str | None = None
    affects: list[str] | None = None        # dimensions whose analyses this affects; None = everything
    dates: list[str] | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _dk(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


def _d(k: int) -> date:
    return date(k // 10000, k // 100 % 100, k % 100)


class DataQuality:
    def __init__(self, con, settings: dict, as_of: date, as_of_evidence: dict, data_version=None):
        self.con = con
        self.cfg = settings.get("data_quality", {})
        self.as_of, self.evidence = as_of, as_of_evidence
        self.data_version = data_version or (lambda tables: None)
        self._cache: dict = {}

    def _q(self, sql: str, *params):
        cur = self.con.cursor()
        try:
            return cur.execute(sql, list(params)).fetchall()
        finally:
            cur.close()

    def run(self, start: date, end: date, dims: set[str] | None = None,
            tables: set[str] | None = None) -> list[DQResult]:
        tables = tables or {"v_attempts"}
        out: list[DQResult] = []
        if tables & {"v_attempts", "fact_payment_attempts_raw", "v_orders"}:
            key = ("attempts", start, end, self.data_version(["fact_payment_attempts_raw"]))
            if key not in self._cache:
                self._cache[key] = self._attempt_checks(start, end)
            out += self._cache[key]
        if tables & {"dim_merchant"} or (dims or set()) & MERCHANT_DIMS:
            key = ("scd", start, end, self.data_version(["dim_merchant"]))
            if key not in self._cache:
                self._cache[key] = self._scd(start, end)
            out += self._cache[key]
        return [r for r in out if r.severity != "OK"]

    # ------------------------------------------------------------------ attempts
    def _attempt_checks(self, start: date, end: date) -> list[DQResult]:
        lo, hi = _dk(start), _dk(end)
        win = [start.isoformat(), end.isoformat()]
        res: list[DQResult] = []

        # DQ01 duplicates (raw vs deduplicated), worst day
        rows = self._q("""SELECT date_key, COUNT(*) AS n_raw, COUNT(DISTINCT attempt_id) AS n_dedup
                          FROM fact_payment_attempts_raw WHERE date_key BETWEEN ? AND ? GROUP BY 1""", lo, hi)
        dup = [(k, (r - u) / u) for k, r, u in rows if u]
        bad = sorted([x for x in dup if x[1] > 0], key=lambda x: -x[1])
        if bad:
            k, rate = bad[0]
            total = sum(r - u for _, r, u in rows) / max(sum(u for _, _, u in rows), 1)
            sev = "BLOCK" if total > 0.05 else "WARN" if rate > 0.005 else "INFO"
            res.append(DQResult("DQ01_DUPLICATES", "fact_payment_attempts_raw", win, sev, round(rate, 4), 0.005,
                                f"Raw volume on {_d(k)} is overstated by {rate:.1%} (replayed records). The analysis "
                                f"uses the deduplicated view v_attempts, so the numbers shown are not affected.",
                                "USED_DEDUP_VIEW", dates=[_d(k).isoformat() for k, _ in bad]))

        # DQ02 null dimension keys (by day, per key)
        for col, label, dims in (("merchant_key", "merchant", sorted(MERCHANT_DIMS)),
                                 ("payment_method_key", "payment method", ["payment_method"]),
                                 ("issuer_key", "issuer", ["issuer_bank"])):
            r = self._q(f"""SELECT date_key, AVG(CASE WHEN {col} IS NULL THEN 1.0 ELSE 0 END) AS z, COUNT(*)
                            FROM v_attempts WHERE date_key BETWEEN ? AND ? GROUP BY 1""", lo, hi)
            days = [(k, z) for k, z, _ in r if z and z > 0]
            if not days:
                continue
            n = sum(c for _, _, c in r)
            overall = sum(z * c for _, z, c in r) / n
            worst_k, worst = max(days, key=lambda x: x[1])
            sev = "BLOCK" if overall > 0.10 else "WARN" if overall > 0.02 or worst > 0.02 else "INFO"
            res.append(DQResult("DQ02_NULL_KEYS", "v_attempts", win, sev, round(overall, 4), 0.02,
                                f"{overall:.1%} of attempts in the window have no {col} (up to {worst:.1%} on "
                                f"{_d(worst_k)}). Breakdowns by {label} attributes show them as an unknown bucket, so "
                                f"segment totals undercount by that share.",
                                "SHOWN_AS_UNKNOWN", affects=dims,
                                dates=[_d(k).isoformat() for k, z in days if z > 0.02]))

        # DQ03 orphan foreign keys
        orphans, n = self._q("""SELECT COUNT(*) FILTER (WHERE a.merchant_key IS NOT NULL AND m.merchant_key IS NULL)
                                      + COUNT(*) FILTER (WHERE pm.payment_method_key IS NULL)
                                      + COUNT(*) FILTER (WHERE i.issuer_key IS NULL AND a.issuer_key IS NOT NULL),
                                      COUNT(*)
                               FROM v_attempts a LEFT JOIN dim_merchant m USING (merchant_key)
                               LEFT JOIN dim_payment_method pm USING (payment_method_key)
                               LEFT JOIN dim_issuer_bank i USING (issuer_key)
                               WHERE a.date_key BETWEEN ? AND ?""", lo, hi)[0]
        if n and orphans:
            rate = orphans / n
            res.append(DQResult("DQ03_ORPHAN_FK", "v_attempts", win, "WARN" if rate > 0.01 else "INFO",
                                round(rate, 4), 0.01, f"{rate:.2%} of attempts reference keys missing from "
                                                      f"dimension tables."))

        # DQ04 freshness / DQ05 late-arriving: days after as_of are incomplete and excluded
        inc = self.evidence.get("incomplete_days") or []
        if inc and end >= self.as_of - timedelta(days=2):
            days = [str(_d(x["date_key"])) for x in inc]
            res.append(DQResult("DQ05_LATE_ARRIVING", "fact_payment_attempts_raw", win, "WARN",
                                min(x["ratio_to_median"] for x in inc), 0.85,
                                f"Data is complete up to {self.as_of}. {', '.join(days)} is still arriving (volume at "
                                f"{min(x['ratio_to_median'] for x in inc):.0%} of normal, many attempts still "
                                f"pending), so it is excluded; a drop on that day would not be real.",
                                "EXCLUDED_INCOMPLETE_DAYS", dates=days))
        # within the window: a day far below normal volume
        r = self._q("""SELECT date_key, COUNT(*) FROM v_attempts WHERE date_key BETWEEN ? AND ? GROUP BY 1 ORDER BY 1""",
                    _dk(start - timedelta(days=28)), hi)
        series = [(k, c) for k, c in r]
        flagged_low, flagged_high = [], []
        for i, (k, c) in enumerate(series):
            if k < lo or i < 7:
                continue
            hist = [x for _, x in series[max(0, i - 28):i]]
            med = statistics.median(hist)
            mad = statistics.median([abs(x - med) for x in hist]) or 1
            z = (c - med) / (1.4826 * mad)
            if z > 3 and c / med >= 1.25:
                flagged_high.append((k, c / med))
            elif z < -3 and c / med <= 0.75:
                flagged_low.append((k, c / med))
        if flagged_high or flagged_low:
            parts = []
            if flagged_high:
                parts.append("unusually high volume on " + ", ".join(f"{_d(k)} ({x:.1f}x)" for k, x in flagged_high[:5]))
            if flagged_low:
                parts.append("unusually low volume on " + ", ".join(f"{_d(k)} ({x:.0%})" for k, x in flagged_low[:5]))
            ev = self._q("""SELECT DISTINCT COALESCE(event_name, holiday_name) FROM dim_date
                            WHERE date_key IN (SELECT UNNEST(?::INTEGER[])) AND COALESCE(event_name, holiday_name) IS NOT NULL""",
                         [k for k, _ in flagged_high + flagged_low])
            note = f" Calendar: {', '.join(e[0] for e in ev)}." if ev else ""
            res.append(DQResult("DQ06_VOLUME_ANOMALY", "v_attempts", win, "INFO", None, 3.0,
                                "Daily volume z-score > 3: " + "; ".join(parts) + "." + note,
                                dates=[_d(k).isoformat() for k, _ in flagged_high + flagged_low]))

        # DQ07 value ranges / DQ08 status validity
        neg, big, lat, bad_status, succ_fail = self._q("""
            SELECT COUNT(*) FILTER (WHERE amount_paise < 0), COUNT(*) FILTER (WHERE amount_paise > 100000000),
                   COUNT(*) FILTER (WHERE latency_ms < 0),
                   COUNT(*) FILTER (WHERE status NOT IN ('SUCCESS','FAILED','TIMEOUT','PENDING','USER_CANCELLED')),
                   COUNT(*) FILTER (WHERE status = 'SUCCESS' AND failure_reason_key IS NOT NULL)
            FROM v_attempts WHERE date_key BETWEEN ? AND ?""", lo, hi)[0]
        if neg or big or lat:
            res.append(DQResult("DQ07_VALUE_RANGE", "v_attempts", win, "WARN", float(neg + big + lat), 0,
                                f"Out-of-range values: {neg} negative amounts, {big} above ₹10 lakh, {lat} negative "
                                f"latencies."))
        if bad_status or succ_fail:
            res.append(DQResult("DQ08_STATUS_VALIDITY", "v_attempts", win, "WARN", float(bad_status + succ_fail), 0,
                                f"{bad_status} attempts with an unknown status, {succ_fail} successes carrying a "
                                f"failure reason."))
        return res

    def _scd(self, start: date, end: date) -> list[DQResult]:
        rows = self._q("""SELECT valid_from, COUNT(DISTINCT merchant_id) FROM dim_merchant
                          WHERE valid_from BETWEEN ? AND ? AND merchant_id IN
                                (SELECT merchant_id FROM dim_merchant GROUP BY 1 HAVING COUNT(*) > 1)
                          GROUP BY 1 ORDER BY 1""", start, end)
        if not rows:
            return []
        desc = ", ".join(f"{n} on {d}" for d, n in rows)
        return [DQResult("DQ10_SCD_CHANGE", "dim_merchant", [start.isoformat(), end.isoformat()], "INFO",
                         float(sum(n for _, n in rows)), None,
                         f"Merchant attributes changed inside the window ({desc} merchants reclassified). Segment "
                         f"breakdowns use the attributes as of each transaction, so a merchant can appear in two "
                         f"segments.", affects=["merchant_segment", "category", "region"],
                         dates=[str(d) for d, _ in rows])]


def worst(results: list[DQResult], dims: set[str] | None = None) -> str:
    """Highest severity among checks that affect the given analysis."""
    sev = "OK"
    for r in results:
        if r.affects is not None and dims is not None and not (set(r.affects) & dims):
            if r.severity == "BLOCK":
                continue
        if SEVERITY[r.severity] > SEVERITY[sev]:
            sev = r.severity
    return sev
