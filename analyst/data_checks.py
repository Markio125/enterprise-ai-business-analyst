"""Verify that every planted anomaly is detectable in the warehouse and
compute the ground-truth numbers used by the evaluation harness.

All numbers are computed with SQL against the generated warehouse, never
from generator internals, so they reflect what an analyst can actually see.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import duckdb
import pandas as pd


@dataclass
class Check:
    anomaly: str
    name: str
    passed: bool
    observed: object
    expectation: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    truth: dict = field(default_factory=dict)

    def add(self, anomaly: str, name: str, passed: bool, observed, expectation: str) -> None:
        self.checks.append(Check(anomaly, name, bool(passed), observed, expectation))

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)

    def as_dict(self) -> dict:
        return {"checks": [asdict(c) for c in self.checks], "ground_truth": self.truth}


def _dk(d: date | str) -> int:
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return int(d.strftime("%Y%m%d"))


def _r(x, n=4):
    return None if x is None else round(float(x), n)


SR_EXCL_CANCEL = ("COUNT(*) FILTER (WHERE status = 'SUCCESS') * 1.0 / "
                  "NULLIF(COUNT(*) FILTER (WHERE status <> 'USER_CANCELLED'), 0)")


def mix_rate_decomposition(df: pd.DataFrame, seg: str = "segment") -> tuple[pd.DataFrame, dict]:
    """df columns: period ('P0'/'P1'), <seg>, denom, succ. Symmetric (exact) decomposition."""
    p = df.pivot_table(index=seg, columns="period", values=["denom", "succ"], aggfunc="sum", fill_value=0)
    d0, d1 = p[("denom", "P0")], p[("denom", "P1")]
    s0, s1 = p[("succ", "P0")], p[("succ", "P1")]
    w0, w1 = d0 / d0.sum(), d1 / d1.sum()
    r0 = (s0 / d0.where(d0 > 0)).fillna(0)
    r1 = (s1 / d1.where(d1 > 0)).fillna(0)
    out = pd.DataFrame({"w0": w0, "w1": w1, "r0": r0, "r1": r1, "denom_p0": d0, "denom_p1": d1})
    out["mix_effect_pp"] = (w1 - w0) * (r0 + r1) / 2 * 100
    out["rate_effect_pp"] = (r1 - r0) * (w0 + w1) / 2 * 100
    out["total_pp"] = out["mix_effect_pp"] + out["rate_effect_pp"]
    R0, R1 = s0.sum() / d0.sum(), s1.sum() / d1.sum()
    totals = {
        "sr_p0": R0, "sr_p1": R1, "delta_pp": (R1 - R0) * 100,
        "mix_effect_pp": out["mix_effect_pp"].sum(), "rate_effect_pp": out["rate_effect_pp"].sum(),
    }
    return out.sort_values("total_pp"), totals


def run_checks(con: duckdb.DuckDBPyConnection, cfg: dict) -> Report:
    rep = Report()
    q = lambda sql, *a: con.execute(sql, list(a)).df()  # noqa: E731
    one = lambda sql, *a: con.execute(sql, list(a)).fetchone()  # noqa: E731
    anom = cfg["anomalies"]
    as_of = date.fromisoformat(cfg["as_of_complete_date"])
    rep.truth["as_of_complete_date"] = as_of.isoformat()
    rep.truth["timezone"] = cfg["timezone"]

    # ------------------------------------------------------------ integrity
    n_raw, n_dedup, dmin, dmax = one(
        "SELECT (SELECT COUNT(*) FROM fact_payment_attempts_raw), COUNT(*), MIN(date_key), MAX(date_key) FROM v_attempts")
    rep.truth["row_counts"] = {"fact_payment_attempts_raw": n_raw, "v_attempts": n_dedup}
    rep.add("BASE", "attempt volume", 700_000 <= n_dedup <= 1_500_000 or cfg.get("_small"), n_dedup, "0.7M-1.5M attempts")
    rep.add("BASE", "date range", (dmin, dmax) == (_dk(cfg["start_date"]), _dk(cfg["end_date"])), [dmin, dmax], "start..end")
    dupe_ids = one("SELECT COUNT(*) - COUNT(DISTINCT attempt_id) FROM v_attempts")[0]
    rep.add("BASE", "v_attempts unique attempt_id", dupe_ids == 0, dupe_ids, "0 duplicates")
    orphans = one("""SELECT COUNT(*) FROM v_attempts a
        LEFT JOIN dim_payment_method p USING (payment_method_key) LEFT JOIN dim_issuer_bank i USING (issuer_key)
        LEFT JOIN dim_acquirer q USING (acquirer_key)
        WHERE p.payment_method_key IS NULL OR i.issuer_key IS NULL OR q.acquirer_key IS NULL""")[0]
    rep.add("BASE", "dimension keys resolve", orphans == 0, orphans, "0 orphan keys")
    bad = one("SELECT COUNT(*) FROM v_attempts WHERE status='SUCCESS' AND failure_reason_key IS NOT NULL")[0]
    rep.add("BASE", "no SUCCESS with failure reason", bad == 0, bad, "0")
    bad = one("SELECT COUNT(*) FROM fact_settlements WHERE net_amount_paise <> gross_amount_paise - mdr_fee_paise - gst_on_fee_paise")[0]
    rep.add("BASE", "settlement net = gross - fee - gst", bad == 0, bad, "0")

    # -------------------------------------------------------------- A9 denominators
    full = q(f"""
        SELECT COUNT(*) FILTER (WHERE status='SUCCESS')*1.0/COUNT(*) AS attempt_level_all,
               {SR_EXCL_CANCEL} AS attempt_level_excl_user_cancel,
               COUNT(*) FILTER (WHERE a.status='SUCCESS')*1.0 /
                 NULLIF(COUNT(*) FILTER (WHERE a.status='SUCCESS' OR NOT COALESCE(f.is_customer_attributable, FALSE)), 0)
                 AS technical_success_rate
        FROM v_attempts a LEFT JOIN dim_failure_reason f USING (failure_reason_key)
        WHERE date_key <= ?""", _dk(as_of)).iloc[0]
    order_sr = one("SELECT AVG(is_success) FROM v_orders WHERE date_key <= ?", _dk(as_of))[0]
    a9 = {k: _r(v) for k, v in full.items()} | {"order_level": _r(order_sr),
                                                  "window": [cfg["start_date"], as_of.isoformat()]}
    rep.truth["A9_denominators"] = a9
    rep.add("A9", "order-level SR well above attempt-level SR",
            a9["order_level"] - a9["attempt_level_excl_user_cancel"] >= 0.04,
            {k: a9[k] for k in ("attempt_level_all", "attempt_level_excl_user_cancel", "order_level")},
            ">= 4pp gap")

    # -------------------------------------------------------------- A1 mix shift
    if anom.get("A1_mix_shift", {}).get("enabled"):
        a1 = anom["A1_mix_shift"]
        p1s = date.fromisoformat(a1["start"])
        length = (as_of - p1s).days + 1
        p0s, p0e = p1s - timedelta(days=length), p1s - timedelta(days=1)
        periods = {"P0": [p0s.isoformat(), p0e.isoformat()], "P1": [p1s.isoformat(), as_of.isoformat()]}
        period_sql = f"CASE WHEN date_key BETWEEN {_dk(p0s)} AND {_dk(p0e)} THEN 'P0' ELSE 'P1' END"
        base = f"""FROM v_attempts a JOIN dim_payment_method pm USING (payment_method_key)
                   WHERE date_key BETWEEN {_dk(p0s)} AND {_dk(as_of)}"""
        agg = "COUNT(*) FILTER (WHERE status <> 'USER_CANCELLED') AS denom, COUNT(*) FILTER (WHERE status='SUCCESS') AS succ"
        by_sub = q(f"SELECT {period_sql} AS period, pm.sub_method AS segment, {agg} {base} GROUP BY ALL")
        dec, tot = mix_rate_decomposition(by_sub)
        by_method = q(f"SELECT {period_sql} AS period, pm.method AS segment, {agg} {base} GROUP BY ALL")
        dec_m, tot_m = mix_rate_decomposition(by_method)
        orders = q(f"""SELECT {period_sql} AS period, AVG(is_success) AS sr, COUNT(*) AS n
                       FROM v_orders WHERE date_key BETWEEN {_dk(p0s)} AND {_dk(as_of)} GROUP BY 1 ORDER BY 1""")
        qb = q(f"""SELECT m.merchant_name, {period_sql} AS period, COUNT(*) AS n
                   FROM v_attempts a JOIN dim_payment_method pm USING (payment_method_key)
                   LEFT JOIN dim_merchant m USING (merchant_key)
                   WHERE date_key BETWEEN {_dk(p0s)} AND {_dk(as_of)} AND pm.sub_method='UPI_COLLECT'
                   GROUP BY ALL""")
        piv = qb.pivot_table(index="merchant_name", columns="period", values="n", aggfunc="sum", fill_value=0)
        incr = (piv["P1"] - piv["P0"]).sort_values(ascending=False)
        top_merchant, top_share = incr.index[0], float(incr.iloc[0] / incr[incr > 0].sum())

        coll = dec.loc["UPI_COLLECT"]
        top = dec["total_pp"].abs().idxmax()
        rep.truth["A1_mix_shift"] = {
            "question": "Why did success rate drop in the last two weeks?",
            "metric": "success_rate (attempt level, excluding user-cancelled)",
            "periods": periods,
            "overall": {k: _r(v) for k, v in tot.items()},
            "by_sub_method": {
                seg: {c: _r(row[c]) for c in ("w0", "w1", "r0", "r1", "mix_effect_pp", "rate_effect_pp", "total_pp")}
                | {"denom_p0": int(row["denom_p0"]), "denom_p1": int(row["denom_p1"])}
                for seg, row in dec.iterrows()},
            "by_method_overall": {k: _r(v) for k, v in tot_m.items()},
            "by_method": {seg: {c: _r(row[c]) for c in ("w0", "w1", "r0", "r1", "mix_effect_pp", "rate_effect_pp")}
                          for seg, row in dec_m.iterrows()},
            "interpretation_code": "MIX_DOMINANT",
            "top_driver": {"dimension": "sub_method", "segment": top},
            "order_level_sr": {r.period: _r(r.sr) for r in orders.itertuples()},
            "incremental_upi_collect_top_merchant": {"merchant_name": top_merchant, "share_of_increase": _r(top_share)},
            "note": "By `method`, UPI shows a rate decline because UPI mixes intent (~91%) and collect (~77%); "
                    "by `sub_method` the change is almost pure mix.",
        }
        rep.add("A1", "overall SR declines", tot["delta_pp"] <= -1.5, _r(tot["delta_pp"], 2), "<= -1.5pp")
        rep.add("A1", "mix effect dominates", abs(tot["mix_effect_pp"]) >= 0.8 * abs(tot["delta_pp"]),
                {"mix": _r(tot["mix_effect_pp"], 2), "rate": _r(tot["rate_effect_pp"], 2)}, "|mix| >= 80% of |delta|")
        rep.add("A1", "rate effect near zero", abs(tot["rate_effect_pp"]) <= 0.35, _r(tot["rate_effect_pp"], 3), "|rate| <= 0.35pp")
        rep.add("A1", "UPI_COLLECT share jumps", coll["w1"] - coll["w0"] >= 0.15,
                [_r(coll["w0"], 3), _r(coll["w1"], 3)], "share +15pp or more")
        rep.add("A1", "UPI_COLLECT SR flat", abs(coll["r1"] - coll["r0"]) <= 0.01,
                [_r(coll["r0"], 4), _r(coll["r1"], 4)], "|delta SR| <= 1pp")
        rep.add("A1", "top driver is UPI_COLLECT", top == "UPI_COLLECT", top, "UPI_COLLECT")
        rep.add("A1", "campaign merchant explains the increase",
                top_merchant == a1["merchant_name"] and top_share >= 0.7, [top_merchant, _r(top_share, 3)],
                f"{a1['merchant_name']} >= 70% of incremental UPI_COLLECT")

    # -------------------------------------------------------------- A2 issuer outage
    if anom.get("A2_issuer_outage", {}).get("enabled"):
        a2 = anom["A2_issuer_outage"]
        d = date.fromisoformat(a2["date"])
        hrs = (a2["start_hour"], a2["end_hour"])
        win = q(f"""SELECT i.bank_code = ? AS is_target, date_key = ? AS is_day, {SR_EXCL_CANCEL} AS sr, COUNT(*) n
            FROM v_attempts a JOIN dim_issuer_bank i USING (issuer_key) JOIN dim_payment_method pm USING (payment_method_key)
            WHERE pm.method <> 'WALLET' AND hour_ist >= ? AND hour_ist < ? AND date_key BETWEEN ? AND ?
            GROUP BY ALL""", a2["issuer_code"], _dk(d), hrs[0], hrs[1], _dk(d - timedelta(days=7)), _dk(d))
        get = lambda t, dd: float(win[(win.is_target == t) & (win.is_day == dd)].sr.iloc[0])  # noqa: E731
        sr_out, sr_base, sr_others = get(True, True), get(True, False), get(False, True)
        code = one("""SELECT f.response_code FROM v_attempts a JOIN dim_issuer_bank i USING (issuer_key)
            JOIN dim_payment_method pm USING (payment_method_key) JOIN dim_failure_reason f USING (failure_reason_key)
            WHERE i.bank_code = ? AND date_key = ? AND hour_ist >= ? AND hour_ist < ? AND pm.method = 'CARD'
            GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1""", a2["issuer_code"], _dk(d), *hrs)[0]
        bank = one("SELECT bank_name FROM dim_issuer_bank WHERE bank_code = ?", a2["issuer_code"])[0]
        rep.truth["A2_issuer_outage"] = {
            "question": f"What happened to card success on {d:%B} {d.day}?",
            "issuer": {"bank_code": a2["issuer_code"], "bank_name": bank},
            "date": d.isoformat(), "window_ist": f"{hrs[0]:02d}:00-{hrs[1]:02d}:00",
            "sr_in_window": _r(sr_out), "sr_same_hours_prior_7d": _r(sr_base), "other_issuers_sr_in_window": _r(sr_others),
            "top_card_response_code": code,
        }
        rep.add("A2", "target issuer SR collapses in window", sr_out <= 0.45, _r(sr_out, 3), "<= 45%")
        rep.add("A2", "target issuer normal otherwise", sr_base >= 0.80, _r(sr_base, 3), ">= 80%")
        rep.add("A2", "other issuers unaffected", sr_others >= 0.80, _r(sr_others, 3), ">= 80%")
        rep.add("A2", "card failures show code 91", code == "91", code, "91")

    # -------------------------------------------------------------- A3 acquirer degradation
    if anom.get("A3_acquirer_degradation", {}).get("enabled"):
        a3 = anom["A3_acquirer_degradation"]
        s, e = date.fromisoformat(a3["start"]), date.fromisoformat(a3["end"])
        ln = (e - s).days + 1
        b0, b1 = s - timedelta(days=ln), s - timedelta(days=1)
        t = q(f"""SELECT acq.acquirer_code, CASE WHEN date_key >= ? THEN 'window' ELSE 'before' END AS w,
                COUNT(*) FILTER (WHERE status='TIMEOUT')*1.0/COUNT(*) AS timeout_rate, {SR_EXCL_CANCEL} AS sr, COUNT(*) n
            FROM v_attempts a JOIN dim_acquirer acq USING (acquirer_key) JOIN dim_payment_method pm USING (payment_method_key)
            WHERE pm.method IN ('CARD','EMI') AND pm.flow = 'CARD_NOT_PRESENT' AND date_key BETWEEN ? AND ?
            GROUP BY ALL""", _dk(s), _dk(b0), _dk(e))
        piv = t.pivot_table(index="acquirer_code", columns="w", values=["timeout_rate", "sr"])
        res = {acq: {"timeout_rate_before": _r(piv.loc[acq, ("timeout_rate", "before")]),
                     "timeout_rate_window": _r(piv.loc[acq, ("timeout_rate", "window")]),
                     "sr_before": _r(piv.loc[acq, ("sr", "before")]), "sr_window": _r(piv.loc[acq, ("sr", "window")])}
               for acq in piv.index}
        rep.truth["A3_acquirer_degradation"] = {
            "question": "Which acquirer had the worst card-not-present performance in late September and why?",
            "window": [s.isoformat(), e.isoformat()], "baseline": [b0.isoformat(), b1.isoformat()],
            "top_driver": {"dimension": "acquirer", "segment": a3["acquirer_code"]},
            "by_acquirer": res,
        }
        tgt = res[a3["acquirer_code"]]
        diff = tgt["timeout_rate_window"] - tgt["timeout_rate_before"]
        others = [v["timeout_rate_window"] - v["timeout_rate_before"] for k, v in res.items() if k != a3["acquirer_code"]]
        rep.add("A3", "target acquirer timeout rate rises", diff >= 0.04, _r(diff, 4), ">= +4pp")
        rep.add("A3", "other acquirers stable", all(abs(x) <= 0.01 for x in others), [_r(x, 4) for x in others], "|delta| <= 1pp")

    # -------------------------------------------------------------- A4 duplicates
    if anom.get("A4_duplicates", {}).get("enabled"):
        a4 = anom["A4_duplicates"]
        dd = q("""SELECT r.date_key, r.n_raw, d.n_dedup, (r.n_raw - d.n_dedup)*1.0/d.n_dedup AS dup_rate
                  FROM (SELECT date_key, COUNT(*) n_raw FROM fact_payment_attempts_raw GROUP BY 1) r
                  JOIN (SELECT date_key, COUNT(*) n_dedup FROM v_attempts GROUP BY 1) d USING (date_key)""")
        tgt = dd[dd.date_key == _dk(a4["date"])].iloc[0]
        other_max = dd[dd.date_key != _dk(a4["date"])].dup_rate.max()
        rep.truth["A4_duplicates"] = {
            "date": a4["date"], "raw_rows": int(tgt.n_raw), "deduplicated_rows": int(tgt.n_dedup),
            "duplicate_rate": _r(tgt.dup_rate), "eval_EV07_transactions_on_date": int(tgt.n_dedup),
        }
        rep.add("A4", "duplicates on target date", 0.02 <= tgt.dup_rate <= 0.04, _r(tgt.dup_rate, 4), "2-4%")
        rep.add("A4", "no duplicates elsewhere", other_max == 0, _r(other_max, 4), "0")

    # -------------------------------------------------------------- A5 null merchant keys
    if anom.get("A5_null_merchant", {}).get("enabled"):
        a5 = anom["A5_null_merchant"]
        nn = q("""SELECT date_key, AVG(CASE WHEN merchant_key IS NULL THEN 1 ELSE 0 END) AS null_rate,
                         AVG(CASE WHEN merchant_key IS NULL AND source_system='POS_SWITCH' THEN 1 ELSE 0 END) AS pos_null
                  FROM v_attempts GROUP BY 1""")
        inwin = nn.date_key.between(_dk(a5["start"]), _dk(a5["end"]))
        seg = q("""SELECT COALESCE(m.segment, 'UNKNOWN') AS segment, SUM(amount_paise)/100.0 AS gmv_inr, COUNT(*) AS n
                   FROM v_attempts a LEFT JOIN dim_merchant m USING (merchant_key)
                   WHERE a.status='SUCCESS' AND date_key BETWEEN ? AND ? GROUP BY 1 ORDER BY 2 DESC""",
                _dk(a5["start"]), _dk(a5["end"]))
        rep.truth["A5_null_merchant"] = {
            "window": [a5["start"], a5["end"]], "source_system": a5["source"],
            "null_rate_in_window": _r(nn[inwin].null_rate.mean()),
            "eval_EV13_gmv_by_segment": {r.segment: round(r.gmv_inr, 2) for r in seg.itertuples()},
        }
        rep.add("A5", "null merchant keys in window", nn[inwin].null_rate.mean() >= 0.03,
                _r(nn[inwin].null_rate.mean(), 4), ">= 3%")
        rep.add("A5", "no null merchant keys elsewhere", nn[~inwin].null_rate.max() == 0,
                _r(nn[~inwin].null_rate.max(), 4), "0")

    # -------------------------------------------------------------- A6 finance disagreement
    if anom.get("A6_finance_source", {}).get("enabled"):
        mstart = as_of.replace(day=1)
        lo, hi = _dk(mstart), _dk(as_of)
        lo_utc, hi_utc = mstart.isoformat(), (as_of + timedelta(days=1)).isoformat()
        dashboard = one("SELECT SUM(amount_paise)/100.0 FROM v_attempts WHERE status='SUCCESS' AND date_key BETWEEN ? AND ?", lo, hi)[0]
        by_status = q("""SELECT s.settlement_status, SUM(s.gross_amount_paise)/100.0 AS amt
                         FROM fact_settlements s JOIN v_attempts a USING (attempt_id)
                         WHERE a.date_key BETWEEN ? AND ? GROUP BY 1""", lo, hi)
        st = dict(zip(by_status.settlement_status, by_status.amt))
        settled_ist = st.get("SETTLED", 0.0)
        settled_utc = one("""SELECT SUM(s.gross_amount_paise)/100.0 FROM fact_settlements s JOIN v_attempts a USING (attempt_id)
                             WHERE s.settlement_status='SETTLED' AND a.created_at_utc >= ?::TIMESTAMP AND a.created_at_utc < ?::TIMESTAMP""",
                          lo_utc, hi_utc)[0]
        missing = sorted(q("""SELECT DISTINCT m.merchant_id FROM fact_settlements s JOIN dim_merchant m USING (merchant_key)
                              WHERE s.settlement_status = 'SETTLED'
                              EXCEPT SELECT DISTINCT merchant_id FROM finance_daily_settlement_summary""").merchant_id)
        excl = 0.0
        if missing:
            excl = one(f"""SELECT COALESCE(SUM(s.gross_amount_paise)/100.0, 0) FROM fact_settlements s
                JOIN v_attempts a USING (attempt_id) JOIN dim_merchant m ON s.merchant_key = m.merchant_key
                WHERE s.settlement_status='SETTLED' AND a.created_at_utc >= ?::TIMESTAMP AND a.created_at_utc < ?::TIMESTAMP
                AND m.merchant_id IN ({",".join("?" * len(missing))})""", lo_utc, hi_utc, *missing)[0]
        finance = float(one("SELECT SUM(gross_amount_inr) FROM finance_daily_settlement_summary WHERE summary_date BETWEEN ? AND ?",
                            mstart, as_of)[0])
        residual = finance - (settled_utc - excl)
        factors = {
            "not_yet_settled_PENDING": st.get("PENDING", 0.0),
            "REVERSED_excluded": st.get("REVERSED", 0.0),
            "settlement_FAILED_excluded": st.get("FAILED", 0.0),
            "ON_HOLD_excluded": st.get("ON_HOLD", 0.0),
            "timezone_IST_vs_UTC_day": settled_ist - settled_utc,
            "merchants_missing_in_finance": excl,
        }
        rep.truth["A6_source_disagreement"] = {
            "question": "Why does finance's settled amount for this month differ from the dashboard GMV?",
            "window": [mstart.isoformat(), as_of.isoformat()],
            "dashboard_gmv_inr": round(dashboard, 2), "finance_gross_inr": round(finance, 2),
            "diff_inr": round(dashboard - finance, 2), "diff_pct": _r((dashboard - finance) / dashboard * 100, 3),
            "explained_by_inr": {k: round(v, 2) for k, v in factors.items()},
            "residual_inr": round(residual, 2),
            "finance_missing_mids": missing,
            "fact_settlements_settled_ist_inr": round(settled_ist, 2),
        }
        rep.add("A6", "sources disagree materially", abs(dashboard - finance) / dashboard >= 0.005,
                _r((dashboard - finance) / dashboard * 100, 3), ">= 0.5% difference")
        rep.add("A6", "difference fully explained", abs(residual) < 1.0, round(residual, 2), "|residual| < INR 1")
        rep.add("A6", "timezone factor non-zero", abs(factors["timezone_IST_vs_UTC_day"]) > 0,
                round(factors["timezone_IST_vs_UTC_day"], 2), "!= 0")

    # -------------------------------------------------------------- A7 late-arriving data
    if anom.get("A7_late_data", {}).get("enabled"):
        a7 = anom["A7_late_data"]
        d = date.fromisoformat(a7["date"])
        daily = q(f"""SELECT date_key, COUNT(*) n, AVG(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) pending_rate,
                      {SR_EXCL_CANCEL} AS sr FROM v_attempts WHERE date_key BETWEEN ? AND ? GROUP BY 1 ORDER BY 1""",
                  _dk(d - timedelta(days=7)), _dk(d))
        last = daily[daily.date_key == _dk(d)].iloc[0]
        prior = daily[daily.date_key != _dk(d)]
        vol_ratio = last.n / prior.n.median()
        rep.truth["A7_late_data"] = {
            "question": "Did success rate drop yesterday?",
            "date": d.isoformat(), "volume_vs_prior_7d_median": _r(vol_ratio), "pending_rate": _r(last.pending_rate),
            "prior_pending_rate": _r(prior.pending_rate.mean()), "sr": _r(last.sr), "prior_sr_mean": _r(prior.sr.mean()),
            "correct_interpretation": "Incomplete data (late ingestion / unconfirmed successes), not a real decline.",
        }
        rep.add("A7", "last day volume short", vol_ratio <= 0.85, _r(vol_ratio, 3), "<= 85% of prior median")
        rep.add("A7", "pending spike on last day", last.pending_rate >= 0.05 and prior.pending_rate.max() <= 0.01,
                [_r(last.pending_rate, 4), _r(prior.pending_rate.max(), 4)], ">= 5% vs <= 1%")
        rep.add("A7", "apparent SR drop on last day", prior.sr.mean() - last.sr >= 0.05,
                _r((last.sr - prior.sr.mean()) * 100, 2), "<= -5pp")

    # -------------------------------------------------------------- A8 sale event
    if anom.get("A8_sale_event", {}).get("enabled"):
        a8 = anom["A8_sale_event"]
        s, e = date.fromisoformat(a8["start"]), date.fromisoformat(a8["end"])
        ln = (e - s).days + 1
        t = q(f"""SELECT CASE WHEN date_key >= ? THEN 'sale' ELSE 'before' END w, COUNT(*)*1.0/{ln} AS per_day, {SR_EXCL_CANCEL} sr
                  FROM v_attempts WHERE date_key BETWEEN ? AND ? GROUP BY 1""",
              _dk(s), _dk(s - timedelta(days=ln)), _dk(e)).set_index("w")
        ratio = t.loc["sale", "per_day"] / t.loc["before", "per_day"]
        dsr = (t.loc["sale", "sr"] - t.loc["before", "sr"]) * 100
        rep.truth["A8_sale_event"] = {"name": a8["name"], "window": [a8["start"], a8["end"]],
                                      "volume_ratio_vs_prior_week": _r(ratio), "sr_delta_pp": _r(dsr, 3)}
        rep.add("A8", "sale volume spike", ratio >= 1.4, _r(ratio, 3), ">= 1.4x")
        rep.add("A8", "sale SR dip", dsr <= -0.5, _r(dsr, 3), "<= -0.5pp")

    # -------------------------------------------------------------- A10 reclassification
    if anom.get("A10_reclassification", {}).get("enabled"):
        a10 = anom["A10_reclassification"]
        cut = date.fromisoformat(a10["date"])
        n_hist = one("SELECT COUNT(*) FROM dim_merchant WHERE NOT is_current")[0]
        seg = q("""SELECT CASE WHEN date_key < ? THEN 'before' ELSE 'after' END w, m.segment, COUNT(DISTINCT m.merchant_id) n
                   FROM v_attempts a JOIN dim_merchant m USING (merchant_key)
                   WHERE date_key BETWEEN ? AND ? GROUP BY ALL""",
                _dk(cut), _dk(cut - timedelta(days=7)), _dk(cut + timedelta(days=6)))
        piv = seg.pivot_table(index="segment", columns="w", values="n", fill_value=0)
        rep.truth["A10_reclassification"] = {
            "date": a10["date"], "merchants_moved": n_hist, "from": a10["from_segment"], "to": a10["to_segment"],
            "active_merchants_by_segment_week_before_after": {s: {"before": int(r["before"]), "after": int(r["after"])}
                                                              for s, r in piv.iterrows()},
        }
        rep.add("A10", "SCD history rows", n_hist == a10["count"], n_hist, f"{a10['count']}")
        mm = piv.loc[a10["to_segment"]]
        rep.add("A10", "target segment gains merchants", mm["after"] - mm["before"] >= a10["count"] * 0.8,
                [int(mm["before"]), int(mm["after"])], f"+~{a10['count']}")

    # -------------------------------------------------------------- extra eval answers
    last_m_end = as_of.replace(day=1) - timedelta(days=1)
    last_m_start = last_m_end.replace(day=1)
    west = q("""SELECT m.merchant_name, m.merchant_id, SUM(amount_paise)/100.0 AS gmv_inr
                FROM v_attempts a JOIN dim_merchant m USING (merchant_key)
                WHERE a.status='SUCCESS' AND m.region='WEST' AND date_key BETWEEN ? AND ?
                GROUP BY ALL ORDER BY gmv_inr DESC LIMIT 5""", _dk(last_m_start), _dk(last_m_end))
    rep.truth["eval_answers"] = {
        "EV04_top5_west_merchants_by_gmv_last_month": {
            "window": [last_m_start.isoformat(), last_m_end.isoformat()],
            "rows": [{"merchant_name": r.merchant_name, "merchant_id": r.merchant_id, "gmv_inr": round(r.gmv_inr, 2)}
                     for r in west.itertuples()]},
    }
    return rep
