# Enterprise AI Business Analyst

An AI analyst for payments data. You ask a business question in plain English (*"Why did our success rate drop in the last two weeks?"*). It works out which data and metrics to use, generates and **validates** SQL, runs it read-only, splits the change into segments, and answers with **query evidence, charts, caveats, and a clear separation of facts, hypotheses and recommendations**.

The full design is in [`docs/SPEC.md`](docs/SPEC.md).

## Status

| # | Segment | Status |
|---|---|---|
| 1 | **Data foundation**: synthetic warehouse + planted anomalies + ground truth | ✅ done |
| 2 | Semantic layer + access policies | ⏳ next |
| 3 | SQL safety (sqlglot validator, row-level security rewrite, cost guard) + read-only executor | ⬜ |
| 4 | LLM pipeline: parse → ambiguity check → plan → SQL generation → repair loop | ⬜ |
| 5 | Analysis engine: mix/rate decomposition, driver ranking, drill-down, data-quality checks, reconciliation | ⬜ |
| 6 | Answer composer: facts / hypotheses / recommendations, number grounding, confidence | ⬜ |
| 7 | API (FastAPI + SSE) + metadata DB | ⬜ |
| 8 | UI | ⬜ |
| 9 | Eval harness, demo, write-up | ⬜ |

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt

python scripts/generate_data.py   # builds data/warehouse.duckdb (~1.05M attempts, ~15 s)
python scripts/verify_data.py     # checks every planted anomaly, writes fixtures/ground_truth.json
pytest -q                         # fast tests at reduced scale
```

`python scripts/generate_data.py --scale 0.2` builds a smaller warehouse for quick iteration. Output is deterministic for a given seed and config.

## The data (segment 1)

A synthetic Indian payments warehouse in DuckDB covering **2026-07-01 → 2026-10-28**, stored as UTC timestamps with IST business days. All merchants, banks, acquirers and people are **fictional**.

```
                    dim_date
                       │
dim_merchant (SCD2) ───┼─── dim_payment_method
dim_acquirer ──── fact_payment_attempts_raw ──── dim_issuer_bank
                       │            └──────────── dim_failure_reason
            v_attempts (deduplicated) / v_orders (order-level outcome)
                       │
       fact_settlements ── fact_refunds
                       │
finance_daily_settlement_summary   ← independent "finance" source that intentionally disagrees
```

| Object | Grain | Notes |
|---|---|---|
| `fact_payment_attempts_raw` | attempt | Raw ingestion, **contains replayed duplicates** |
| `v_attempts` | attempt | Deduplicated view, the default analysis target |
| `v_orders` | order | Did the customer eventually pay, after retries? |
| `fact_settlements` | settled transaction | MDR, GST on fee, net, T+1 (weekends roll to Monday), status |
| `fact_refunds` | refund | ~2% of settled transactions |
| `finance_daily_settlement_summary` | merchant × **UTC** day | Rupees, SETTLED only, two MIDs unmapped |
| `dim_merchant` | merchant version | SCD2: `merchant_key` is point-in-time; join facts on `merchant_key` **without** filtering `is_current` |
| `_data_versions` | table | Row counts and load time, used later for cache invalidation |

Amounts are stored in **paise** (`BIGINT`). Divide by 100 for rupees.

### Planted anomalies (the questions the analyst must get right)

| ID | Anomaly | Where | What a correct answer says |
|---|---|---|---|
| A1 | **Mix shift (Simpson's paradox)** | P1 = Oct 14–27 vs P0 = Sep 30–Oct 13 | Overall SR falls ~2.6pp, but **~97% of the drop is mix**: `QuickBazaar` floods UPI Collect (share 13% → 33%), and no sub-method's SR moves |
| A2 | Issuer outage | Meridian Bank, Sep 10, 18:00–23:00 IST | SR ~31% vs ~87% normally; card code `91` |
| A3 | Acquirer degradation | Acquirer Beta, Sep 20–27, card-not-present | Timeout rate up ~5.6pp; other acquirers flat |
| A4 | Duplicate records | Oct 5 | 3% of raw rows replayed; use `v_attempts` |
| A5 | Missing merchant keys | Oct 18–20, POS switch | ~8% of rows have no merchant; segment totals undercount |
| A6 | Two sources disagree | October month-to-date | Dashboard GMV vs finance ledger differ ~3.5%, fully explained by reversed / failed / on-hold settlements, two unmapped merchants, and the UTC vs IST day boundary |
| A7 | Late-arriving data | Oct 28 | ~79% of normal volume and a PENDING spike make SR look down ~12pp. The data is incomplete; it is not a real decline |
| A8 | Sale event | "Freedom Sale", Aug 10–16 | Volume ×1.8, SR −1.5pp |
| A9 | Denominator trap | Always | Attempt-level SR 87.5%, order-level 93.2%, technical 93.9% |
| A10 | Segment reclassification | Sep 1 | 30 merchants move SMB → MID_MARKET |

The exact numbers are in [`fixtures/ground_truth.json`](fixtures/ground_truth.json). They are computed **with SQL against the warehouse**, not from generator internals, so they match what an analyst can actually see. `scripts/verify_data.py` asserts 34 checks.

### Changes from the spec

- Banks and acquirers are fictional (e.g. *Meridian Bank*, *Acquirer Beta*), so no outage is attributed to a real institution. The A2 outage moved to Sep 10 so it doesn't contaminate the A1 comparison windows.
- Diwali 2026 (Nov 8) falls outside the data window, so the sale event is a fictional August "Freedom Sale".
- "Today" for relative time is the **last complete day, 2026-10-27**. Oct 28 is deliberately partial.
- `dim_terminal` is omitted (stretch). `is_tokenized` lives on the fact table.
- With SCD2, joining on `merchant_key AND is_current` would drop history. The semantic layer (segment 2) joins on `merchant_key` for point-in-time attributes, and via `merchant_id` when current attributes are wanted.

## Repository layout

```
analyst/
  reference.py      static reference data (fictional banks, merchants, failure codes)
  datagen.py        deterministic generator → DuckDB
  data_checks.py    anomaly verification + ground-truth computation + mix/rate decomposition
config/generator.yaml   every generator knob (volumes, rates, anomaly windows and magnitudes)
scripts/            generate_data.py, verify_data.py
fixtures/ground_truth.json
tests/
docs/SPEC.md        full system specification
```

## Assumptions & limitations (so far)

- Synthetic data only. Rates and distributions are plausible but not calibrated to any real processor.
- Anomalies are isolated in time so each has one clean, verifiable explanation.
- The generator favours clarity over realism in places. For example, merchant success-rate noise is centred so the A1 campaign merchant is exactly average, which makes A1 a near-pure mix effect.
