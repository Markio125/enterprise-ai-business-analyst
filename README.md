# Enterprise AI Business Analyst

An AI analyst for payments data. You ask a business question in plain English (*"Why did our success rate drop in the last two weeks?"*). It works out which data and metrics to use, generates and **validates** SQL, runs it read-only, splits the change into segments, and answers with **query evidence, charts, caveats, and a clear separation of facts, hypotheses and recommendations**.

The full design is in [`docs/SPEC.md`](docs/SPEC.md).

## Status

| # | Segment | Status |
|---|---|---|
| 1 | **Data foundation**: synthetic warehouse + planted anomalies + ground truth | ✅ done |
| 2 | **Semantic layer + access policies**: metrics, dimensions, synonyms, time expressions, RBAC, row-level scope, catalog | ✅ done |
| 3 | **SQL safety**: sqlglot validator (S01–S16), row-level security rewrite, cost guard, sealed read-only executor | ✅ done |
| 4 | LLM pipeline: parse → ambiguity check → plan → SQL generation → repair loop | ⏳ next |
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
python scripts/check_semantic.py  # validates the semantic layer + policies, re-derives the ground truth through it
python scripts/check_safety.py    # ground truth through the SQL gate + an attack catalog (34 checks)
pytest -q                         # fast tests at reduced scale
```

Try the semantic layer as different users:

```bash
python scripts/catalog.py --list-users
python scripts/catalog.py --user u_west                       # what this user may ask about (LLM grounding block)
python scripts/query.py --metric success_rate --by sub_method --time "last two weeks" --compare
python scripts/query.py --user u_west --metric gmv_inr --by merchant --time "last month" --top 5
python scripts/query.py --user u_ops --metric mdr_revenue_inr # -> REFUSED_ACCESS with alternatives

# free-form SQL through the safety gate (what LLM-generated SQL will go through)
python scripts/run_sql.py --user u_west --explain "SELECT COUNT(*) FROM v_attempts WHERE date_key >= 20261001"
python scripts/run_sql.py --user u_ops "SELECT 1; DROP TABLE v_attempts"
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
- With SCD2, joining on `merchant_key AND is_current` would drop history. The semantic layer joins on `merchant_key` for point-in-time attributes, and via `merchant_id` when current attributes are wanted.
- Policies: the `finance` role also gets `v_attempts` / `dim_payment_method` so it can reconcile dashboard GMV against the ledger (A6). `merchant_user` is scoped by `merchant_id`, not `merchant_key`, because keys change on SCD reclassification. All roles are denied `dim_merchant.legal_entity_id`.
- Out-of-scope requests default to **refuse** (the spec left this open).

## The semantic layer (segment 2)

[`semantic/model.yaml`](semantic/model.yaml) is the single source of business meaning. The LLM is grounded on it, and a deterministic compiler ([`analyst/semantic.py`](analyst/semantic.py)) turns `metric x dimensions x filters x time` into parameterised SQL.

| Concept | What it holds |
|---|---|
| **Entities** | `attempts`, `orders`, `settlements`, `refunds`, `finance_summary`: table, grain, time bases, joins, measures |
| **Measures** | Aggregate SQL fragments per entity (`success_count`, `non_cancelled_count`, ...) |
| **Metrics** | 27 metrics built from measures. `success_rate` has 4 **variants** (attempt-level excl. cancels *(default)*, all attempts, technical, order-level) and the variant used is always disclosed. `failure_rate` is its complement. |
| **Dimensions** | 40 dimensions with synonyms (*bank* -> `issuer_bank`, *mode* -> `payment_method`) |
| **Ambiguous terms** | *revenue* (GMV vs MDR), *volume* (count vs value), *conversion*, *settled amount* (two ledgers), *segment* (then vs now) |
| **Time expressions** | [`analyst/timeparse.py`](analyst/timeparse.py) resolves *last two weeks*, *last month*, *Q3*, *since 14 Oct* ... in IST against the data's **as-of date**. Vague phrases (*recently*) are flagged ambiguous with a default. |
| **As-of date** | Detected from the data: the last day whose volume is >= 0.85 x the trailing 14-day median. This finds **Oct 27** and rejects the partial Oct 28 (A7). |

How the compiler stays correct:

- **Joins are inferred** from the aliases a query touches, with their dependencies, and nothing else is joined. For example, the current segment needs `merchant` and then `merchant_current`.
- **Merchant attributes are point-in-time by default.** The facts join `dim_merchant` on `merchant_key` with no `is_current` filter. `merchant_current` gives today's attributes.
- **Settlements have two time bases.** `settlement_date` is the default and `transaction_date` is the alternative. The basis used is disclosed because "settled this month" is ambiguous.
- **Filter values are always bound parameters** and are never inlined into SQL.
- **`model.validate(con)`** checks every alias, column, measure, synonym collision and join against the live warehouse, then executes every metric and every metric x dimension pair.

`scripts/check_semantic.py` **re-derives the segment-1 ground truth through the compiler**: A9's four success-rate variants, A1's windows and per-method rates, A6's dashboard GMV / finance ledger / settled amounts, EV04's top-5 WEST merchants under row-level scope, and A10's point-in-time vs current segment. All 26 checks pass.

### Access control

[`config/policies.yaml`](config/policies.yaml) defines the roles and [`analyst/policies.py`](analyst/policies.py) enforces them. This is layer 1 (semantic pre-check). The SQL validator in segment 3 re-checks the generated SQL against the same rules.

| Role | Can see | Scope |
|---|---|---|
| `exec` | everything aggregated; no row-level output | — |
| `ops_analyst` | attempts, orders, dimensions, raw table; **no** settlements / finance / refunds / MDR | — |
| `regional_manager_west` | as ops | `region = WEST` |
| `account_manager` | as ops | `account_manager = <user name>` |
| `finance` | settlements, refunds, finance ledger, MDR, plus dashboard GMV (for reconciliation) | — |
| `merchant_user` | own attempts / orders; **acquirer is confidential** | `merchant_id = <user's MID>` |

- A metric is allowed only if every table and column it touches is allowed, including the joins it needs, and it isn't `restricted_to` other roles. Example: `mdr_revenue_inr` is restricted to finance and exec.
- **Row-level scope** is injected by the system as bound parameters. The LLM is never trusted to apply it. Scope joins may use tables the role can't query directly: a merchant user is filtered by `merchant_id` without access to `dim_merchant`.
- **Failing closed:**
  - A missing user attribute means the request is refused.
  - If a scope can't be applied to an entity, that entity is denied.
  - Rows with no merchant (A5) are excluded from scoped results, and the results disclose it.
- Asking explicitly about something outside your scope (a WEST manager asking about NORTH) is **refused** by default. `out_of_scope_behaviour: scope_and_disclose` switches to running within scope and saying so.
- Refusals follow the `REFUSED_ACCESS` contract: denied items, reasons, and alternatives the user *can* ask.
- [`analyst/catalog.py`](analyst/catalog.py) builds the **role-filtered catalog**: metrics, dimensions, sample values, ambiguous terms, and what is not accessible. The same structure becomes `GET /catalog` in segment 7 and the grounding block in the LLM prompt in segment 4. Values that would leak across a scope (other regions or merchants) are left out.

## SQL safety and execution (segment 3)

Every query derived from user input goes through one gate: [`analyst/executor.py`](analyst/executor.py) `QueryGateway`. That covers LLM-written SQL and the semantic compiler's own SQL.

```
SQL (+ params) -> SqlValidator (sqlglot AST) -> PASS + HMAC seal -> ReadOnlyExecutor (sandboxed DuckDB)
                        |-> FAIL_UNSAFE / FAIL_ACCESS_DENIED / FAIL_REPAIRABLE / NEEDS_NARROWING
```

| Rule | What [`analyst/sqlsafety.py`](analyst/sqlsafety.py) does | On fail |
|---|---|---|
| S01–S03 | Exactly one statement, rooted in SELECT/UNION. No DML, DDL, COPY, ATTACH, PRAGMA, SET, INSTALL/LOAD, CALL, EXPORT, SELECT INTO, DESCRIBE/SHOW anywhere in the tree. Statements sqlglot can't parse are classified by their leading keyword. | UNSAFE |
| S04 | No file, URL, environment or catalog functions (`read_csv`, `glob`, `getenv`, `duckdb_*`, `pragma_*` ...) and no `FROM 'file.csv'` | UNSAFE |
| S05 | Tables must be in the role's allow-list. Cross-database references, CTEs that **shadow real tables**, and recursive CTEs are blocked. System catalogs are denied. | ACCESS_DENIED / UNSAFE |
| S06 | Every column is **resolved to its base table** through CTEs, subqueries and `SELECT *` expansion (sqlglot `qualify` + scopes), then checked against the role. Denied dimensions also deny their fact-side join keys: a merchant user can't `GROUP BY acquirer_key`. | ACCESS_DENIED |
| S07 | Large facts need a time filter. Windows over `max_time_window_days` need narrowing. | REPAIRABLE / NARROWING |
| S08, S09 | No `SELECT *` on facts. At most 4 joins. No cross, comma or `ON TRUE` joins. | REPAIRABLE |
| S10 | Non-aggregated queries get a LIMIT (default 1000, cap 5000). Roles without row-level output (exec) are capped at 50. | AUTO_FIX |
| S11 | **Row-level security**: every reference to a merchant-linked table (found from the schema: `merchant_key` / `merchant_id` columns) is replaced by a filtered subquery on `main.dim_merchant`. That includes references inside CTEs, subqueries and UNION branches, and the security predicate itself refers to the real table by its schema-qualified name. | AUTO_FIX |
| S12 | DuckDB `EXPLAIN (FORMAT JSON)` estimates. Too many rows scanned, or a single operator that's too big (join explosions), gives NEEDS_NARROWING with concrete options, or REJECT if configured. | NARROWING |
| S13, S14, S16 | Warnings: division without NULLIF. Metric SQL that doesn't match the semantic layer's measure definitions, which is also **disclosed** in the answer. Use of the raw table with duplicates. | WARN |
| S15 | The validator is the only gate: the executor **refuses SQL without a valid HMAC seal** tied to the exact SQL text and the principal. | — |

The executor is the second line of defence:
- The connection is read-only, with `enable_external_access=false`, extension auto-install/load disabled, a memory limit, and `lock_configuration=true`.
- Each statement has a timeout enforced through `interrupt()`.
- Results are capped at the row limit and flagged as truncated.
- Each result carries a `data_version` (base tables resolved through views) for caching later.

Filter values from the semantic path are inlined by sqlglot as escaped literals. Row-level scope values are escaped the same way: an account manager called `x' OR '1'='1` matches nothing.

`scripts/check_safety.py` runs the A9, A6 and EV04 ground truth through the full gate, plus 25 attacks (stacked statements, file exfiltration, CTE shadowing, PII through `SELECT *` and CTEs, routing leaks, join explosions ...). All 34 pass. `tests/test_sqlsafety.py` has about 100 adversarial cases.

## Repository layout

```
analyst/
  reference.py      static reference data (fictional banks, merchants, failure codes)
  datagen.py        deterministic generator → DuckDB
  data_checks.py    anomaly verification + ground-truth computation + mix/rate decomposition
  semantic.py       semantic model loader, validator, term lookup, metric -> SQL compiler
  timeparse.py      relative time expressions + as-of (last complete day) detection
  policies.py       RBAC, row-level scope, refusal contract
  catalog.py        role-filtered catalog (JSON + LLM-prompt markdown)
  sqlsafety.py      SQL validator (S01-S16), RLS rewrite, cost guard, seal
  executor.py       sandboxed read-only executor + QueryGateway (the single query entry point)
  settings.py       loads config/analyst.yaml
semantic/model.yaml     entities, measures, metrics, dimensions, ambiguous terms, glossary
config/generator.yaml   every generator knob (volumes, rates, anomaly windows and magnitudes)
config/policies.yaml    roles, row-level scopes, demo users
config/analyst.yaml     warehouse limits, cost controls, SQL-safety lists
scripts/            generate_data.py, verify_data.py, check_semantic.py, check_safety.py, catalog.py, query.py, run_sql.py
fixtures/ground_truth.json
tests/
docs/SPEC.md        full system specification
```

## Assumptions & limitations (so far)

- Synthetic data only. Rates and distributions are plausible but not calibrated to any real processor.
- Anomalies are isolated in time so each has one clean, verifiable explanation.
- Every `v_attempts` query scans the whole raw table: the view's deduplication window blocks filter pushdown. That is fine at 1M rows. At scale, the deduplicated table should be materialised so time filters cut the scan, and the cost guard would show the difference.
- The time-filter rule (S07) is best-effort: any predicate on a fact's time column counts. The EXPLAIN cost guard (S12) is the backstop.
- The generator favours clarity over realism in places. For example, merchant success-rate noise is centred so the A1 campaign merchant is exactly average, which makes A1 a near-pure mix effect.
