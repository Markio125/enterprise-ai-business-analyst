"""Deterministic synthetic payments warehouse generator.

Builds the DuckDB star schema described in docs/SPEC.md §4 and plants the
anomalies from §5 so the analyst has real, known-answer patterns to find.

Usage:
    python scripts/generate_data.py [--config config/generator.yaml] [--out data/warehouse.duckdb] [--scale 1.0]
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from analyst import reference as ref

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "generator.yaml"
DEFAULT_OUT = PROJECT_ROOT / "data" / "warehouse.duckdb"
IST_OFFSET = pd.Timedelta(hours=5, minutes=30)

S = ref.SUB_INDEX
CH = {c: i for i, c in enumerate(ref.CHANNELS)}
DV = {d: i for i, d in enumerate(ref.DEVICES)}


def _d(s: str) -> date:
    return date.fromisoformat(s)


def load_config(path: Path | str = DEFAULT_CONFIG) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def config_sha(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class DayContext:
    day: date
    sale: bool
    outage: dict | None
    a3: bool


class WarehouseGenerator:
    def __init__(self, cfg: dict, scale: float = 1.0):
        self.cfg = cfg
        self.scale = scale
        self.rng = np.random.default_rng(cfg["seed"])
        self.anom = {k: v for k, v in cfg["anomalies"].items() if v.get("enabled", True)}
        self.start = _d(cfg["start_date"])
        self.end = _d(cfg["end_date"])
        self.days = [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]
        snap_ist = pd.Timestamp(cfg["snapshot_at_ist"])
        self.snapshot_utc = snap_ist - IST_OFFSET
        self.snapshot_date = snap_ist.date()
        self._prepare_static()

    # ------------------------------------------------------------------ static
    def _prepare_static(self) -> None:
        cfg, beh = self.cfg, self.cfg["behaviour"]
        self.base_sr = np.array([cfg["base_success_rate"][s] for s in ref.SUBS])
        self.card_like = np.array([s in ref.CARD_LIKE_SUBS for s in ref.SUBS])
        self.latency_med = np.array([ref.LATENCY_MEDIAN_MS[s] for s in ref.SUBS], dtype=float)
        self.sub_group = np.array([ref.FAILURE_GROUPS.index(ref.SUB_GROUP[s]) for s in ref.SUBS])

        def mix(d):
            return np.array([d.get(s, 0.0) for s in ref.SUBS]) / sum(d.values())

        self.mix_online = mix(cfg["payment_mix"]["online"])
        self.mix_pos = mix(cfg["payment_mix"]["pos"])

        iss = cfg["issuers"]
        self.issuers = pd.DataFrame(iss, columns=["bank_code", "bank_name", "bank_type", "share", "adj_pp"])
        self.iss_share = self.issuers["share"].to_numpy() / self.issuers["share"].sum()
        self.iss_adj = self.issuers["adj_pp"].to_numpy() / 100.0

        acq = cfg["acquirers"]
        self.acquirers = pd.DataFrame(acq, columns=["acquirer_code", "acquirer_name", "share", "adj_pp"])
        self.acq_share = self.acquirers["share"].to_numpy() / self.acquirers["share"].sum()
        self.acq_adj = self.acquirers["adj_pp"].to_numpy() / 100.0

        self.device_adj = np.array([beh["device_adj_pp"][d] for d in ref.DEVICES]) / 100.0
        hp = np.array(cfg["volume"]["hourly_profile"], dtype=float)
        self.hourly = hp / hp.sum()

        self.fail_codes = {g: (list(d.keys()), np.array(list(d.values())) / sum(d.values()))
                           for g, d in ref.FAILURE_DIST.items()}

    # --------------------------------------------------------------- merchants
    def build_merchants(self) -> None:
        cfg, rng = self.cfg["merchants"], self.rng
        segs: list[str] = []
        for seg, n in cfg["segments"].items():
            segs += [seg] * n
        n = len(segs)
        segs_arr = np.array(segs)

        weight = np.empty(n)
        for seg, (mu, sd) in cfg["segment_weight_lognormal"].items():
            m = segs_arr == seg
            weight[m] = rng.lognormal(mu, sd, m.sum())

        is_pos = rng.random(n) < cfg["pos_share"]
        cat = np.where(
            is_pos,
            rng.choice(ref.CATEGORIES_POS[0], n, p=ref.CATEGORIES_POS[1]),
            rng.choice(ref.CATEGORIES_ONLINE[0], n, p=ref.CATEGORIES_ONLINE[1]),
        ).astype(object)
        integ = np.where(is_pos, "POS", rng.choice(["ONLINE_PG", "PAYMENT_LINK", "SDK"], n, p=[0.7, 0.15, 0.15])).astype(object)

        city_w = np.array([c[4] for c in ref.CITIES], dtype=float)
        city_idx = rng.choice(len(ref.CITIES), n, p=city_w / city_w.sum())

        # special merchants
        smb_online = np.where((segs_arr == "SMB") & ~is_pos)[0]
        qb = int(smb_online[0])
        cat[qb], integ[qb] = "ECOMMERCE", "ONLINE_PG"
        city_idx[qb] = [c[0] for c in ref.CITIES].index("Jaipur")
        mm_pos = np.where((segs_arr == "MID_MARKET") & is_pos)[0]
        axis = int(mm_pos[0])
        cat[axis] = "RETAIL"

        names: list[str] = []
        used: set[str] = set()
        for i in range(n):
            if i == qb:
                name = self.anom.get("A1_mix_shift", {}).get("merchant_name", "QuickBazaar")
            elif i == axis:
                name = "Axis Lifestyle Stores"   # deliberately collides with a bank-like word (entity ambiguity)
            else:
                for _ in range(50):
                    name = f"{rng.choice(ref.NAME_PREFIXES)} {rng.choice(ref.CATEGORY_NOUNS[cat[i]])}"
                    if name not in used:
                        break
                else:
                    name = f"{name} {ref.CITIES[city_idx[i]][0]}"
            used.add(name)
            names.append(name)

        # onboarding dates
        onboarded = np.array([np.datetime64("2020-01-01") + np.timedelta64(int(x), "D")
                              for x in rng.integers(0, (date(2026, 6, 15) - date(2020, 1, 1)).days, n)])
        candidates = np.setdiff1d(np.arange(n), [qb, axis])
        new_idx = rng.choice(candidates, cfg["new_in_period"], replace=False)
        onboarded[new_idx] = [np.datetime64("2026-07-05") + np.timedelta64(int(x), "D")
                              for x in rng.integers(0, 97, len(new_idx))]
        onboarded[qb] = np.datetime64("2025-03-12")

        # merchant-level success adjustment, traffic-weighted mean zero; QuickBazaar exactly average
        adj = rng.normal(0, cfg["merchant_adj_sd_pp"] / 100.0, n)
        adj -= np.average(adj, weights=weight)
        # campaign merchant behaves exactly like the average online merchant, so A1 is a pure mix effect
        online_others = ~is_pos & (np.arange(n) != qb)
        adj[qb] = np.average(adj[online_others], weights=weight[online_others])

        # SCD2 reclassification
        a10 = self.anom.get("A10_reclassification")
        scd = np.zeros(n, bool)
        if a10:
            pool = np.where((segs_arr == a10["from_segment"]) & (onboarded < np.datetime64(a10["date"])))[0]
            pool = np.setdiff1d(pool, [qb])
            scd[rng.choice(pool, a10["count"], replace=False)] = True

        regions = np.array([ref.CITIES[c][2] for c in city_idx])
        am = np.array([rng.choice(ref.ACCOUNT_MANAGERS[r]) for r in regions])
        mcc = np.array([rng.choice(ref.CATEGORY_MCC[c]) for c in cat])
        risk = rng.choice(["LOW", "MEDIUM", "HIGH"], n, p=[0.7, 0.25, 0.05])
        risk[cat == "GAMING"] = "HIGH"

        self.n_merchants = n
        self.qb_idx, self.axis_idx = qb, axis
        self.m_weight = weight
        self.m_seg = segs_arr
        self.m_is_pos = is_pos
        self.m_integ = np.array([ref.INTEGRATIONS.index(x) for x in integ])
        self.m_adj = adj
        self.m_onboarded = onboarded
        self.m_scd = scd
        self.m_ticket_med = np.array([ref.CATEGORY_TICKET[c][0] for c in cat], dtype=float)
        self.m_ticket_sig = np.array([ref.CATEGORY_TICKET[c][1] for c in cat], dtype=float)
        self.m_mid = np.array([f"MID{100001 + i}" for i in range(n)])
        self.key_v1 = np.arange(1, n + 1)
        self.key_v2 = np.zeros(n, dtype=int)
        self.key_v2[scd] = n + 1 + np.arange(scd.sum())
        self.scd_date = np.datetime64(a10["date"]) if a10 else None

        base = pd.DataFrame({
            "merchant_id": self.m_mid, "merchant_name": names,
            "legal_entity_id": [f"LE{700000 + i}" for i in range(n)],
            "mcc": mcc, "category": cat.astype(str), "segment": segs_arr,
            "integration_type": integ.astype(str),
            "city": [ref.CITIES[c][0] for c in city_idx], "state": [ref.CITIES[c][1] for c in city_idx],
            "region": regions, "city_tier": [ref.CITIES[c][3] for c in city_idx],
            "onboarded_date": pd.to_datetime(onboarded).date, "status": "ACTIVE",
            "account_manager": am, "risk_band": risk,
        })
        v1 = base.copy()
        v1.insert(0, "merchant_key", self.key_v1)
        v1["valid_from"] = v1["onboarded_date"]
        v1["valid_to"] = None
        v1["is_current"] = True
        if a10:
            cut = _d(a10["date"])
            v1.loc[scd, "valid_to"] = cut - timedelta(days=1)
            v1.loc[scd, "is_current"] = False
            v2 = base[scd].copy()
            v2.insert(0, "merchant_key", self.key_v2[scd])
            v2["segment"] = a10["to_segment"]
            v2["valid_from"] = cut
            v2["valid_to"] = None
            v2["is_current"] = True
            v1 = pd.concat([v1, v2], ignore_index=True)
        self.dim_merchant = v1

    # ------------------------------------------------------------ dimensions
    def build_dims(self) -> None:
        rows = []
        key = 1
        self.pm_key: dict[tuple, int] = {}
        for s in ref.SUBS:
            if s in ref.CARD_LIKE_SUBS:
                for ni, net in enumerate(ref.NETWORKS):
                    if ref.NETWORK_MIX[s][ni] == 0:
                        continue
                    for flow in ("CARD_PRESENT", "CARD_NOT_PRESENT"):
                        rows.append((key, ref.SUB_TO_METHOD[s], ref.SUB_METHOD_LABEL[s], net, "CONSUMER", flow))
                        self.pm_key[(S[s], ni, flow == "CARD_PRESENT")] = key
                        key += 1
            else:
                rows.append((key, ref.SUB_TO_METHOD[s], ref.SUB_METHOD_LABEL[s], None, None, None))
                self.pm_key[(S[s], -1, False)] = key
                key += 1
        self.dim_payment_method = pd.DataFrame(
            rows, columns=["payment_method_key", "method", "sub_method", "card_network", "card_type", "flow"])

        self.dim_issuer_bank = pd.DataFrame({
            "issuer_key": np.arange(1, len(self.issuers) + 1),
            "bank_code": self.issuers["bank_code"], "bank_name": self.issuers["bank_name"],
            "bank_type": self.issuers["bank_type"], "country": "IN",
        })
        self.dim_acquirer = pd.DataFrame({
            "acquirer_key": np.arange(1, len(self.acquirers) + 1),
            "acquirer_code": self.acquirers["acquirer_code"], "acquirer_name": self.acquirers["acquirer_name"],
        })
        fr = pd.DataFrame(ref.FAILURE_REASONS, columns=["response_code", "description", "failure_category",
                                                         "is_retryable", "is_customer_attributable"])
        fr.insert(0, "failure_reason_key", np.arange(1, len(fr) + 1))
        self.dim_failure_reason = fr
        self.reason_key = dict(zip(fr["response_code"], fr["failure_reason_key"]))

        a8 = self.anom.get("A8_sale_event")
        drows = []
        for d in self.days:
            sale = bool(a8 and _d(a8["start"]) <= d <= _d(a8["end"]))
            hol = ref.HOLIDAYS.get(d.isoformat())
            drows.append((int(d.strftime("%Y%m%d")), d, d.strftime("%a"), d.weekday() >= 5,
                          d - timedelta(days=d.weekday()), d.strftime("%Y-%m"),
                          f"{d.year}-Q{(d.month - 1) // 3 + 1}", hol is not None, hol,
                          sale, a8["name"] if sale else None))
        self.dim_date = pd.DataFrame(drows, columns=["date_key", "date", "day_of_week", "is_weekend", "week_start",
                                                     "month", "quarter", "is_holiday_in", "holiday_name",
                                                     "is_sale_event", "event_name"])

    # ------------------------------------------------------------- simulation
    def _in_window(self, day: date, key: str) -> bool:
        a = self.anom.get(key)
        if not a:
            return False
        if "date" in a and "start" not in a:
            return day == _d(a["date"])
        return _d(a["start"]) <= day <= _d(a["end"])

    def _outcome(self, ctx: DayContext, sub, is_pos, issuer, acq, hour, device, madj):
        rng, beh = self.rng, self.cfg["behaviour"]
        n = len(sub)
        card = self.card_like[sub]
        not_wallet = sub != S["WALLET"]
        p = self.base_sr[sub].copy()
        p += np.where(card & is_pos, beh["card_present_bonus_pp"] / 100, 0)
        p += np.where(not_wallet, self.iss_adj[issuer], 0)
        p += np.where(card, self.acq_adj[acq], 0)
        p -= np.where(hour < 5, beh["night_penalty_pp"] / 100, 0)
        p += self.device_adj[device]
        p += madj
        if ctx.sale:
            p -= self.anom["A8_sale_event"]["success_penalty_pp"] / 100

        outage = np.zeros(n, bool)
        if ctx.outage:
            o = ctx.outage
            outage = (issuer == o["issuer_idx"]) & not_wallet & (hour >= o["start_hour"]) & (hour < o["end_hour"])
            p[outage] = o["success_rate"]
        a3 = np.zeros(n, bool)
        extra = 0.0
        if ctx.a3:
            a = self.anom["A3_acquirer_degradation"]
            a3 = card & ~is_pos & (acq == self.a3_acq_idx)
            extra = a["extra_timeout_rate"]
        p = np.clip(p, 0.02, 0.995)
        p_eff = np.where(a3, p - extra, p)

        u = rng.random(n)
        success = u < p_eff
        a3_timeout = a3 & ~success & (u < p)
        cancel = rng.random(n) < np.where(is_pos, beh["cancel_rate"]["pos"], beh["cancel_rate"]["online"])
        pending = rng.random(n) < beh["pending_rate"]
        is_timeout = ~success & ~outage & (a3_timeout | (rng.random(n) < beh["timeout_share_of_failures"]))

        status = np.full(n, "FAILED", dtype=object)
        status[success] = "SUCCESS"
        status[is_timeout] = "TIMEOUT"
        status[pending] = "PENDING"
        status[cancel] = "USER_CANCELLED"

        reason = np.full(n, None, dtype=object)
        failed = status == "FAILED"
        timed = status == "TIMEOUT"
        for gi, g in enumerate(ref.FAILURE_GROUPS):
            mg = self.sub_group[sub] == gi
            idx = np.where(failed & mg & ~outage)[0]
            if len(idx):
                codes, probs = self.fail_codes[g]
                reason[idx] = rng.choice(codes, len(idx), p=probs)
            reason[timed & mg] = ref.TIMEOUT_CODE[g]
            reason[failed & mg & outage] = ref.OUTAGE_CODE[g]
        reason[status == "USER_CANCELLED"] = "UC"

        latency = self.latency_med[sub] * rng.lognormal(0, 0.5, n)
        latency = np.where(timed, 90000 + rng.integers(0, 5000, n), latency)
        return status, reason, np.round(latency).astype(np.int64)

    def _sample_subs(self, is_pos):
        n = len(is_pos)
        return np.where(is_pos, self.rng.choice(len(ref.SUBS), n, p=self.mix_pos),
                        self.rng.choice(len(ref.SUBS), n, p=self.mix_online))

    def _simulate_day(self, i: int, day: date) -> pd.DataFrame:
        rng, cfg, vol = self.rng, self.cfg, self.cfg["volume"]
        ctx = DayContext(day=day, sale=self._in_window(day, "A8_sale_event"), outage=None,
                         a3=self._in_window(day, "A3_acquirer_degradation"))
        if self._in_window(day, "A2_issuer_outage"):
            o = dict(self.anom["A2_issuer_outage"])
            o["issuer_idx"] = int(np.where(self.issuers["bank_code"] == o["issuer_code"])[0][0])
            ctx.outage = o

        active = self.m_onboarded <= np.datetime64(day)
        w = self.m_weight * active
        if ctx.sale:
            w = np.where(~self.m_is_pos, w * self.anom["A8_sale_event"]["online_volume_multiplier"], w)
        wk = vol["weekend_factor"] if day.weekday() >= 5 else 1.0
        noise = float(np.exp(rng.normal(0, vol["daily_noise_sd"])))
        expected = (vol["base_orders_per_day"] * self.scale * (1 + vol["daily_growth"]) ** i * wk * noise
                    * w.sum() / self.m_weight.sum())
        midx = rng.choice(self.n_merchants, size=rng.poisson(expected), p=w / w.sum())
        forced = np.zeros(len(midx), bool)
        a1 = self.anom.get("A1_mix_shift")
        if a1 and day >= _d(a1["start"]):
            n_c = rng.poisson(a1["campaign_orders_per_day"] * self.scale * noise)
            midx = np.concatenate([midx, np.full(n_c, self.qb_idx)])
            forced = np.concatenate([forced, np.ones(n_c, bool)])
        n = len(midx)

        is_pos = self.m_is_pos[midx]
        integ = self.m_integ[midx]
        u = rng.random(n)
        channel = np.select([integ == 0, integ == 2, integ == 3], [CH["POS"], CH["LINK"], CH["APP"]],
                            default=np.where(u < 0.55, CH["APP"], CH["WEB"]))
        u2 = rng.random(n)
        device = np.select(
            [channel == CH["POS"], channel == CH["WEB"], channel == CH["APP"]],
            [DV["POS"], DV["WEB"], np.where(u2 < 0.73, DV["ANDROID"], DV["IOS"])],
            default=np.where(u2 < 0.6, DV["ANDROID"], np.where(u2 < 0.8, DV["IOS"], DV["WEB"])))
        amount = np.maximum(100, np.round(self.m_ticket_med[midx] * rng.lognormal(0, self.m_ticket_sig[midx]) * 100)).astype(np.int64)
        issuer = rng.choice(len(self.iss_share), n, p=self.iss_share)
        hour0 = rng.choice(24, n, p=self.hourly)
        t = hour0 * 3600 + rng.random(n) * 3600
        sub_ord = self._sample_subs(is_pos)
        sub_ord[forced] = S["UPI_COLLECT"]
        madj = self.m_adj[midx]
        order_rec = ~is_pos & (rng.random(n) < 0.03)
        order_tok = ~is_pos & (rng.random(n) < 0.60)

        beh = cfg["behaviour"]
        rec = {k: [] for k in ("oi", "an", "t", "sub", "acq", "status", "reason", "lat")}
        cur = np.arange(n)
        for a in range(1, beh["max_attempts"] + 1):
            sub = sub_ord[cur]
            acq = rng.choice(len(self.acq_share), len(cur), p=self.acq_share)
            hour = np.minimum(t[cur] // 3600, 23).astype(int)
            status, reason, lat = self._outcome(ctx, sub, is_pos[cur], issuer[cur], acq, hour, device[cur], madj[cur])
            for k, v in (("oi", cur), ("an", np.full(len(cur), a)), ("t", t[cur]), ("sub", sub), ("acq", acq),
                         ("status", status), ("reason", reason), ("lat", lat)):
                rec[k].append(v)
            if a == beh["max_attempts"]:
                break
            failed = (status == "FAILED") | (status == "TIMEOUT")
            cancelled = status == "USER_CANCELLED"
            u_retry = rng.random(len(cur))
            retry = (failed & (u_retry < beh["retry_prob_after_failure"])) | (
                cancelled & (u_retry < beh.get("retry_prob_after_cancel", 0.0)))
            nxt = cur[retry]
            if not len(nxt):
                break
            t[nxt] = np.minimum(t[nxt] + rng.uniform(20, 240, len(nxt)), 86399.0)
            switch = rng.random(len(nxt)) < beh["retry_switch_method_prob"]
            new_sub = self._sample_subs(is_pos[nxt])
            sub_ord[nxt] = np.where(switch, new_sub, sub_ord[nxt])
            cur = nxt

        r = {k: np.concatenate(v) for k, v in rec.items()}
        oi = r["oi"]
        sub = r["sub"]
        m = midx[oi]
        pos_a = is_pos[oi]
        card = self.card_like[sub]

        net = np.full(len(sub), -1)
        for s, probs in ref.NETWORK_MIX.items():
            msk = sub == S[s]
            if msk.any():
                net[msk] = rng.choice(len(ref.NETWORKS), msk.sum(), p=probs)
        codes = sub * 100 + (net + 1) * 10 + (card & pos_a)
        lut = {k[0] * 100 + (k[1] + 1) * 10 + int(k[2]): v for k, v in self.pm_key.items()}
        pm_key = pd.Series(codes).map(lut).to_numpy()

        in_v2 = self.m_scd[m] & (np.datetime64(day) >= self.scd_date) if self.scd_date is not None else np.zeros(len(m), bool)
        merchant_key = np.where(in_v2, self.key_v2[m], self.key_v1[m])

        prefix = f"ORD{day:%y%m%d}"
        order_id = prefix + pd.Series(oi).astype(str).str.zfill(6)
        created_ist = pd.Timestamp(day) + pd.to_timedelta(np.round(r["t"] * 1000).astype(np.int64), unit="ms")
        return pd.DataFrame({
            "order_id": order_id.to_numpy(),
            "attempt_no": r["an"].astype(np.int16),
            "created_ist": created_ist,
            "merchant_idx": m,
            "merchant_key": merchant_key,
            "payment_method_key": pm_key,
            "sub": sub,
            "issuer_key": issuer[oi] + 1,
            "acquirer_key": r["acq"] + 1,
            "amount_paise": amount[oi],
            "status": r["status"],
            "reason": r["reason"],
            "latency_ms": r["lat"],
            "is_3ds": card & ~pos_a,
            "is_recurring": order_rec[oi] & card,
            "is_tokenized": order_tok[oi] & card,
            "device_type": np.array(ref.DEVICES)[device[oi]],
            "channel": np.array(ref.CHANNELS)[channel[oi]],
            "source_system": np.where(pos_a, "POS_SWITCH", "PG_CORE"),
        })

    # --------------------------------------------------------------- facts
    def build_attempts(self) -> None:
        self.a3_acq_idx = None
        a3 = self.anom.get("A3_acquirer_degradation")
        if a3:
            self.a3_acq_idx = int(np.where(self.acquirers["acquirer_code"] == a3["acquirer_code"])[0][0])
        frames = [self._simulate_day(i, d) for i, d in enumerate(self.days)]
        df = pd.concat(frames, ignore_index=True)
        df["attempt_id"] = df["order_id"] + "-" + df["attempt_no"].astype(str)
        df["created_at_utc"] = df["created_ist"] - IST_OFFSET
        ist = df["created_ist"]
        df["date_key"] = (ist.dt.year * 10000 + ist.dt.month * 100 + ist.dt.day).astype(np.int32)
        df["hour_ist"] = ist.dt.hour.astype(np.int16)
        lag_s = self.rng.lognormal(np.log(90), 1.0, len(df))
        df["ingested_at_utc"] = df["created_at_utc"] + pd.to_timedelta(np.round(lag_s * 1000).astype(np.int64), unit="ms")
        df["merchant_key_true"] = df["merchant_key"]
        df["merchant_key"] = df["merchant_key"].astype("Int64")

        # A7 — last day only partially loaded
        a7 = self.anom.get("A7_late_data")
        if a7:
            dk = int(_d(a7["date"]).strftime("%Y%m%d"))
            on_day = (df["date_key"] == dk).to_numpy()
            drop = on_day & (self.rng.random(len(df)) < a7["missing_rate"])
            df = df[~drop].reset_index(drop=True)
            on_day = (df["date_key"] == dk).to_numpy()
            pend = on_day & (df["status"] == "SUCCESS").to_numpy() & (self.rng.random(len(df)) < a7["success_pending_rate"])
            df.loc[pend, "status"] = "PENDING"
            df.loc[pend, "reason"] = None
        # ensure nothing was ingested after the snapshot
        df["ingested_at_utc"] = df["ingested_at_utc"].where(df["ingested_at_utc"] <= self.snapshot_utc, self.snapshot_utc)

        # A5 — POS switch bug drops merchant_key
        a5 = self.anom.get("A5_null_merchant")
        if a5:
            lo, hi = (int(_d(a5[k]).strftime("%Y%m%d")) for k in ("start", "end"))
            msk = (df["date_key"].between(lo, hi) & (df["source_system"] == a5["source"])).to_numpy()
            msk = msk & (self.rng.random(len(df)) < a5["rate_within_source"])
            df.loc[msk, "merchant_key"] = pd.NA

        df["failure_reason_key"] = df["reason"].map(self.reason_key).astype("Int64")
        df = df.sort_values(["created_at_utc", "attempt_id"]).reset_index(drop=True)
        self.attempts = df  # clean, one row per attempt

        # A4 — replayed ingestion creates exact duplicates in the raw table
        raw = df
        a4 = self.anom.get("A4_duplicates")
        if a4:
            dk = int(_d(a4["date"]).strftime("%Y%m%d"))
            cand = np.where(df["date_key"].to_numpy() == dk)[0]
            pick = self.rng.choice(cand, int(round(len(cand) * a4["rate"])), replace=False)
            dup = df.iloc[pick].copy()
            dup["ingested_at_utc"] = dup["ingested_at_utc"] + pd.Timedelta(hours=6)
            raw = pd.concat([df, dup], ignore_index=True).sort_values(["created_at_utc", "attempt_id"]).reset_index(drop=True)
        self.attempts_raw = raw

    def build_settlements(self) -> None:
        rng, st = self.rng, self.cfg["settlement"]
        a = self.attempts
        s = a[a["status"] == "SUCCESS"].reset_index(drop=True)
        txn_date = s["created_ist"].dt.normalize()
        sd = txn_date + pd.Timedelta(days=1)
        wd = sd.dt.day_of_week
        sd = sd + pd.to_timedelta(np.where(wd == 5, 2, np.where(wd == 6, 1, 0)), unit="D")
        pending = (sd.dt.date > (self.snapshot_date - timedelta(days=1))).to_numpy()
        statuses = list(st["status_mix"].keys())
        probs = np.array(list(st["status_mix"].values()))
        status = rng.choice(statuses, len(s), p=probs / probs.sum()).astype(object)
        status[pending] = "PENDING"
        mdr = np.array([st["mdr_pct"][ref.SUBS[x]] for x in s["sub"]]) / 100.0
        fee = np.round(s["amount_paise"].to_numpy() * mdr).astype(np.int64)
        gst = np.round(fee * st["gst_on_fee_pct"] / 100.0).astype(np.int64)
        has_utr = np.isin(status, ["SETTLED", "REVERSED"])
        utr = np.where(has_utr, pd.Series(rng.integers(10**11, 10**12, len(s))).astype(str).radd("UTR").to_numpy(), None)
        sdk = (sd.dt.year * 10000 + sd.dt.month * 100 + sd.dt.day).astype("Int64")
        sdk[pending] = pd.NA
        lag = (sd - txn_date).dt.days.astype("Int64")
        lag[pending] = pd.NA
        self.settlements = pd.DataFrame({
            "settlement_id": "STL" + pd.Series(np.arange(1, len(s) + 1)).astype(str).str.zfill(9),
            "attempt_id": s["attempt_id"],
            "merchant_key": s["merchant_key_true"].astype("Int64"),
            "settlement_date_key": sdk,
            "gross_amount_paise": s["amount_paise"],
            "mdr_fee_paise": fee,
            "gst_on_fee_paise": gst,
            "net_amount_paise": s["amount_paise"].to_numpy() - fee - gst,
            "settlement_status": status,
            "settlement_lag_days": lag,
            "utr": utr,
        })
        self._settle_join = s[["attempt_id", "created_at_utc", "merchant_idx", "created_ist"]]

    def build_refunds(self) -> None:
        rng, cfg = self.rng, self.cfg["refunds"]
        st = self.settlements
        settled = st[st["settlement_status"] == "SETTLED"].merge(self._settle_join, on="attempt_id")
        pick = settled.sample(frac=cfg["rate"], random_state=int(self.cfg["seed"])).reset_index(drop=True)
        lagd = rng.integers(1, cfg["max_lag_days"] + 1, len(pick))
        rdate = pick["created_ist"].dt.normalize() + pd.to_timedelta(lagd, unit="D")
        keep = (rdate.dt.date <= self.end).to_numpy()
        pick, rdate = pick[keep].reset_index(drop=True), rdate[keep].reset_index(drop=True)
        full = rng.random(len(pick)) < 0.7
        amt = np.where(full, pick["gross_amount_paise"],
                       np.round(pick["gross_amount_paise"] * rng.uniform(0.2, 0.8, len(pick)))).astype(np.int64)
        self.refunds = pd.DataFrame({
            "refund_id": "RFD" + pd.Series(np.arange(1, len(pick) + 1)).astype(str).str.zfill(8),
            "attempt_id": pick["attempt_id"],
            "merchant_key": pick["merchant_key"],
            "refund_date_key": (rdate.dt.year * 10000 + rdate.dt.month * 100 + rdate.dt.day).astype(np.int32),
            "refund_amount_paise": amt,
            "refund_type": np.where(full, "FULL", "PARTIAL"),
            "refund_status": rng.choice(["PROCESSED", "PENDING", "FAILED"], len(pick), p=[0.95, 0.04, 0.01]),
            "reason": rng.choice(["CUSTOMER_REQUEST", "DISPUTE", "DUPLICATE_CHARGE", "MERCHANT_CANCEL"],
                                 len(pick), p=[0.6, 0.1, 0.1, 0.2]),
        })

    def build_finance(self) -> None:
        """Independent finance ledger: SETTLED only, UTC day of the transaction, rupees, some MIDs unmapped."""
        a6 = self.anom.get("A6_finance_source", {"missing_merchants": 0})
        st = self.settlements
        j = st[st["settlement_status"] == "SETTLED"].merge(self._settle_join, on="attempt_id")
        online_mm = np.where(~self.m_is_pos & (self.m_seg == "MID_MARKET"))[0]
        online_mm = np.setdiff1d(online_mm, [self.qb_idx, self.axis_idx])
        self.finance_missing_mids = sorted(self.m_mid[online_mm[: a6["missing_merchants"]]].tolist())
        j["merchant_id"] = self.m_mid[j["merchant_idx"].to_numpy()]
        j = j[~j["merchant_id"].isin(self.finance_missing_mids)]
        j["summary_date"] = j["created_at_utc"].dt.date
        g = j.groupby(["summary_date", "merchant_id"], as_index=False).agg(
            txn_count=("attempt_id", "size"), gross=("gross_amount_paise", "sum"), net=("net_amount_paise", "sum"))
        g["gross_amount_inr"] = (g["gross"] / 100).round(2)
        g["net_amount_inr"] = (g["net"] / 100).round(2)
        g["source"] = "FINANCE_LEDGER"
        g["loaded_at"] = self.snapshot_utc - pd.Timedelta(hours=1)
        self.finance = g[["summary_date", "merchant_id", "txn_count", "gross_amount_inr", "net_amount_inr",
                          "source", "loaded_at"]].sort_values(["summary_date", "merchant_id"]).reset_index(drop=True)

    # --------------------------------------------------------------- write
    def write(self, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
        con = duckdb.connect(str(out))
        con.execute(DDL)
        fact_cols = ["attempt_id", "order_id", "attempt_no", "created_at_utc", "date_key", "hour_ist", "merchant_key",
                     "payment_method_key", "issuer_key", "acquirer_key", "amount_paise", "status", "failure_reason_key",
                     "latency_ms", "is_3ds", "is_recurring", "is_tokenized", "device_type", "channel",
                     "ingested_at_utc", "source_system"]
        tables = {
            "dim_date": self.dim_date,
            "dim_merchant": self.dim_merchant,
            "dim_payment_method": self.dim_payment_method,
            "dim_issuer_bank": self.dim_issuer_bank,
            "dim_acquirer": self.dim_acquirer,
            "dim_failure_reason": self.dim_failure_reason,
            "fact_payment_attempts_raw": self.attempts_raw[fact_cols].assign(currency="INR"),
            "fact_settlements": self.settlements,
            "fact_refunds": self.refunds,
            "finance_daily_settlement_summary": self.finance,
        }
        for name, df in tables.items():
            cols = ", ".join(con.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{name}' ORDER BY ordinal_position").fetchnumpy()["column_name"])
            con.register("_src", df)
            con.execute(f"INSERT INTO {name} SELECT {cols} FROM _src")
            con.unregister("_src")
        con.execute(VIEWS)
        snap = self.snapshot_utc.to_pydatetime()
        for name, ts_col in [("fact_payment_attempts_raw", "created_at_utc"), ("fact_settlements", None),
                             ("fact_refunds", None), ("finance_daily_settlement_summary", "loaded_at")]:
            cnt = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            mx = con.execute(f"SELECT MAX({ts_col}) FROM {name}").fetchone()[0] if ts_col else None
            con.execute("INSERT INTO _data_versions VALUES (?, ?, ?, ?, ?)",
                        [name, f"{name}@{snap:%Y-%m-%dT%H:%MZ}", cnt, mx, snap])
        con.execute("CHECKPOINT")
        con.close()

    def run(self, out: Path) -> dict:
        t0 = time.time()
        timings = {}
        for step in ("build_merchants", "build_dims", "build_attempts", "build_settlements", "build_refunds", "build_finance"):
            ts = time.time()
            getattr(self, step)()
            timings[step] = round(time.time() - ts, 2)
        ts = time.time()
        self.write(out)
        timings["write"] = round(time.time() - ts, 2)
        return {
            "seed": self.cfg["seed"],
            "scale": self.scale,
            "config_sha": config_sha(self.cfg),
            "output": str(out),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "rows": {
                "fact_payment_attempts_raw": len(self.attempts_raw),
                "v_attempts (deduplicated)": len(self.attempts),
                "fact_settlements": len(self.settlements),
                "fact_refunds": len(self.refunds),
                "finance_daily_settlement_summary": len(self.finance),
                "dim_merchant (incl. SCD versions)": len(self.dim_merchant),
            },
            "special_entities": {
                "campaign_merchant": self.m_mid[self.qb_idx],
                "entity_ambiguity_merchant": self.m_mid[self.axis_idx],
                "finance_missing_mids": self.finance_missing_mids,
                "reclassified_mids": sorted(self.m_mid[self.m_scd].tolist()),
            },
            "timings_sec": timings,
            "total_sec": round(time.time() - t0, 2),
        }


DDL = """
CREATE TABLE dim_date (
  date_key INTEGER PRIMARY KEY, date DATE NOT NULL, day_of_week VARCHAR, is_weekend BOOLEAN,
  week_start DATE, month VARCHAR, quarter VARCHAR, is_holiday_in BOOLEAN, holiday_name VARCHAR,
  is_sale_event BOOLEAN, event_name VARCHAR
);
CREATE TABLE dim_merchant (
  merchant_key INTEGER PRIMARY KEY, merchant_id VARCHAR NOT NULL, merchant_name VARCHAR NOT NULL,
  legal_entity_id VARCHAR, mcc VARCHAR, category VARCHAR, segment VARCHAR, integration_type VARCHAR,
  city VARCHAR, state VARCHAR, region VARCHAR, city_tier VARCHAR, onboarded_date DATE, status VARCHAR,
  account_manager VARCHAR, risk_band VARCHAR, valid_from DATE NOT NULL, valid_to DATE, is_current BOOLEAN NOT NULL
);
CREATE TABLE dim_payment_method (
  payment_method_key INTEGER PRIMARY KEY, method VARCHAR NOT NULL, sub_method VARCHAR,
  card_network VARCHAR, card_type VARCHAR, flow VARCHAR
);
CREATE TABLE dim_issuer_bank (
  issuer_key INTEGER PRIMARY KEY, bank_code VARCHAR UNIQUE, bank_name VARCHAR, bank_type VARCHAR, country VARCHAR
);
CREATE TABLE dim_acquirer (acquirer_key INTEGER PRIMARY KEY, acquirer_code VARCHAR, acquirer_name VARCHAR);
CREATE TABLE dim_failure_reason (
  failure_reason_key INTEGER PRIMARY KEY, response_code VARCHAR, description VARCHAR, failure_category VARCHAR,
  is_retryable BOOLEAN, is_customer_attributable BOOLEAN
);
-- Raw ingested attempts: may contain replayed duplicates (no primary key on purpose)
CREATE TABLE fact_payment_attempts_raw (
  attempt_id VARCHAR NOT NULL, order_id VARCHAR NOT NULL, attempt_no SMALLINT NOT NULL,
  created_at_utc TIMESTAMP NOT NULL, date_key INTEGER NOT NULL, hour_ist SMALLINT,
  merchant_key INTEGER, payment_method_key INTEGER, issuer_key INTEGER, acquirer_key INTEGER,
  amount_paise BIGINT NOT NULL, currency VARCHAR, status VARCHAR NOT NULL, failure_reason_key INTEGER,
  latency_ms INTEGER, is_3ds BOOLEAN, is_recurring BOOLEAN, is_tokenized BOOLEAN,
  device_type VARCHAR, channel VARCHAR, ingested_at_utc TIMESTAMP, source_system VARCHAR
);
CREATE TABLE fact_settlements (
  settlement_id VARCHAR PRIMARY KEY, attempt_id VARCHAR, merchant_key INTEGER, settlement_date_key INTEGER,
  gross_amount_paise BIGINT, mdr_fee_paise BIGINT, gst_on_fee_paise BIGINT, net_amount_paise BIGINT,
  settlement_status VARCHAR, settlement_lag_days SMALLINT, utr VARCHAR
);
CREATE TABLE fact_refunds (
  refund_id VARCHAR PRIMARY KEY, attempt_id VARCHAR, merchant_key INTEGER, refund_date_key INTEGER,
  refund_amount_paise BIGINT, refund_type VARCHAR, refund_status VARCHAR, reason VARCHAR
);
-- Independent source owned by Finance: UTC day of transaction, rupees, SETTLED only
CREATE TABLE finance_daily_settlement_summary (
  summary_date DATE, merchant_id VARCHAR, txn_count INTEGER, gross_amount_inr DECIMAL(18,2),
  net_amount_inr DECIMAL(18,2), source VARCHAR, loaded_at TIMESTAMP
);
CREATE TABLE _data_versions (
  table_name VARCHAR, version VARCHAR, row_count BIGINT, max_event_time TIMESTAMP, loaded_at TIMESTAMP
);
"""

VIEWS = """
-- Deduplicated, analysis-ready attempts (first ingested copy wins)
CREATE VIEW v_attempts AS
SELECT * EXCLUDE (_rn) FROM (
  SELECT f.*, ROW_NUMBER() OVER (PARTITION BY attempt_id ORDER BY ingested_at_utc) AS _rn
  FROM fact_payment_attempts_raw f
) WHERE _rn = 1;

-- Order-level outcome (did the customer eventually pay, after retries?)
CREATE VIEW v_orders AS
SELECT
  order_id,
  MIN(created_at_utc)                          AS first_attempt_at_utc,
  arg_min(date_key, attempt_no)                AS date_key,
  arg_min(merchant_key, attempt_no)            AS merchant_key,
  arg_min(payment_method_key, attempt_no)      AS first_payment_method_key,
  MAX(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS is_success,
  BOOL_AND(status = 'USER_CANCELLED')          AS all_cancelled,
  COUNT(*)                                     AS attempt_count,
  MAX(CASE WHEN status = 'SUCCESS' THEN amount_paise END) AS paid_amount_paise
FROM v_attempts
GROUP BY order_id;
"""


def generate(config_path: Path | str = DEFAULT_CONFIG, out: Path | str = DEFAULT_OUT, scale: float = 1.0) -> dict:
    cfg = load_config(config_path)
    gen = WarehouseGenerator(cfg, scale=scale)
    return gen.run(Path(out))
