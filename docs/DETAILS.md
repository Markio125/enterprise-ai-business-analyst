# Technical reference

How each part of the system works, in more depth than the README. For the reasoning behind the design see [WRITEUP.md](WRITEUP.md); for diagrams see [ARCHITECTURE.md](ARCHITECTURE.md).

## The data


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

The exact numbers are in [`fixtures/ground_truth.json`](../fixtures/ground_truth.json). They are computed **with SQL against the warehouse**, not from generator internals, so they match what an analyst can actually see. `scripts/verify_data.py` asserts 34 checks.

### Differences from the spec

- Banks and acquirers are fictional (e.g. *Meridian Bank*, *Acquirer Beta*), so no outage is attributed to a real institution. The A2 outage moved to Sep 10 so it doesn't contaminate the A1 comparison windows.
- Diwali 2026 (Nov 8) falls outside the data window, so the sale event is a fictional August "Freedom Sale".
- "Today" for relative time is the **last complete day, 2026-10-27**. Oct 28 is deliberately partial.
- `dim_terminal` is omitted (stretch). `is_tokenized` lives on the fact table.
- With SCD2, joining on `merchant_key AND is_current` would drop history. The semantic layer joins on `merchant_key` for point-in-time attributes, and via `merchant_id` when current attributes are wanted.
- Policies: the `finance` role also gets `v_attempts` / `dim_payment_method` so it can reconcile dashboard GMV against the ledger (A6). `merchant_user` is scoped by `merchant_id`, not `merchant_key`, because keys change on SCD reclassification. All roles are denied `dim_merchant.legal_entity_id`.
- Out-of-scope requests default to **refuse** (the spec left this open).
- Planning is deterministic templates rather than an LLM planner: the metric layer covers the SPEC question types exactly, and the LLM writes SQL only for what it can't express. The ambiguity materiality threshold is 5%, not 10%, so the success-rate definitions (about 6-7% apart here) are asked about, as in the demo script.
- The A2 eval question uses the ground-truth date (Sep 10, Meridian Bank), not the spec's Oct 12 / YES Bank.

## The semantic layer


[`semantic/model.yaml`](../semantic/model.yaml) is the single source of business meaning. The LLM is grounded on it, and a deterministic compiler ([`analyst/semantic.py`](../analyst/semantic.py)) turns `metric x dimensions x filters x time` into parameterised SQL.

| Concept | What it holds |
|---|---|
| **Entities** | `attempts`, `orders`, `settlements`, `refunds`, `finance_summary`: table, grain, time bases, joins, measures |
| **Measures** | Aggregate SQL fragments per entity (`success_count`, `non_cancelled_count`, ...) |
| **Metrics** | 27 metrics built from measures. `success_rate` has 4 **variants** (attempt-level excl. cancels *(default)*, all attempts, technical, order-level) and the variant used is always disclosed. `failure_rate` is its complement. |
| **Dimensions** | 40 dimensions with synonyms (*bank* -> `issuer_bank`, *mode* -> `payment_method`) |
| **Ambiguous terms** | *revenue* (GMV vs MDR), *volume* (count vs value), *conversion*, *settled amount* (two ledgers), *segment* (then vs now) |
| **Time expressions** | [`analyst/timeparse.py`](../analyst/timeparse.py) resolves *last two weeks*, *last month*, *Q3*, *since 14 Oct* ... in IST against the data's **as-of date**. Vague phrases (*recently*) are flagged ambiguous with a default. |
| **As-of date** | Detected from the data: the last day whose volume is >= 0.85 x the trailing 14-day median. This finds **Oct 27** and rejects the partial Oct 28 (A7). |

How the compiler stays correct:

- **Joins are inferred** from the aliases a query touches, with their dependencies, and nothing else is joined. For example, the current segment needs `merchant` and then `merchant_current`.
- **Merchant attributes are point-in-time by default.** The facts join `dim_merchant` on `merchant_key` with no `is_current` filter. `merchant_current` gives today's attributes.
- **Settlements have two time bases.** `settlement_date` is the default and `transaction_date` is the alternative. The basis used is disclosed because "settled this month" is ambiguous.
- **Filter values are always bound parameters** and are never inlined into SQL.
- **`model.validate(con)`** checks every alias, column, measure, synonym collision and join against the live warehouse, then executes every metric and every metric x dimension pair.

`scripts/check_semantic.py` **re-derives the segment-1 ground truth through the compiler**: A9's four success-rate variants, A1's windows and per-method rates, A6's dashboard GMV / finance ledger / settled amounts, EV04's top-5 WEST merchants under row-level scope, and A10's point-in-time vs current segment. All 26 checks pass.

### Access control

[`config/policies.yaml`](../config/policies.yaml) defines the roles and [`analyst/policies.py`](../analyst/policies.py) enforces them. This is layer 1 (semantic pre-check). The SQL validator re-checks generated SQL against the same rules.

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
- [`analyst/catalog.py`](../analyst/catalog.py) builds the **role-filtered catalog**: metrics, dimensions, sample values, ambiguous terms, and what is not accessible. The same structure backs `GET /api/v1/catalog` and the grounding block in the LLM prompt. Values that would leak across a scope (other regions or merchants) are left out.

## SQL safety and execution


Every query derived from user input goes through one gate: [`analyst/executor.py`](../analyst/executor.py) `QueryGateway`. That covers LLM-written SQL and the semantic compiler's own SQL.

```
SQL (+ params) -> SqlValidator (sqlglot AST) -> PASS + HMAC seal -> ReadOnlyExecutor (sandboxed DuckDB)
                        |-> FAIL_UNSAFE / FAIL_ACCESS_DENIED / FAIL_REPAIRABLE / NEEDS_NARROWING
```

| Rule | What [`analyst/sqlsafety.py`](../analyst/sqlsafety.py) does | On fail |
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

## Question pipeline


[`analyst/pipeline.py`](../analyst/pipeline.py) `Analyst.ask(question, user_id, session)` runs the SPEC section 7 state machine up to executed queries:

```
PARSING ─┬─ data-change request ───────────────▶ REJECTED_UNSAFE   (before any SQL exists)
         ├─ off-topic / unknown metric / definition ▶ ANSWERED_DIRECT
ACCESS_PRECHECK ── restricted metric, out-of-scope filter ▶ REFUSED_ACCESS
AMBIGUITY_CHECK ── material ambiguity (probe values shown) ▶ AWAITING_CLARIFICATION ─ answer ─▶ re-run
PLANNING ── result larger than the row cap ─────────▶ AWAITING_NARROWING ─ option ─▶ re-run
SQL_GENERATION ⇄ VALIDATION ⇄ EXECUTION (repair loop ≤ 2)
DQ_CHECK + ANALYSIS
COMPOSITION + GROUNDING_CHECK ──▶ ANSWERED | ANSWERED_LOW_CONFIDENCE | ANSWERED_WITH_DQ_BLOCK
```

**LLM proposes, code disposes.**

- **Parsing.** The model (Claude Haiku 5.5 by default) returns JSON in a fixed schema. Structured outputs guarantee the shape. It sees only the user's role-filtered catalog.
  - Code then re-checks every metric and dimension name against the semantic layer. Invalid names go back to the model with the error, up to two retries; after that they become `unknown_terms`.
  - Filter values are snapped to real dimension values.
  - Time phrases are copied verbatim and resolved by `timeparse.py`, never by the model.
- **Access pre-check.** Each metric is authorized with the requested breakdowns and filters.
  - Merchant names are resolved **inside the user's scope**, by a query that runs through the gate with row-level security. An out-of-scope merchant and a non-existent one get the same answer.
  - A fuzzy match is offered as "did you mean …?" and never substituted silently.
- **Ambiguity** ([`analyst/ambiguity.py`](../analyst/ambiguity.py)). Rules decide what is ambiguous; probe queries, run as the user, decide whether it matters.
  - The four success-rate definitions differ by about 6-7% on this data (85.5% vs 93.3%), above the 5% materiality threshold. So a bare "success rate" asks, with each definition's value shown.
  - A vague period ("lately") asks. A missing period uses the last 30 days, disclosed.
  - Business words with different units ("revenue": GMV or MDR) always ask, but only when the user can see more than one reading. For ops, "revenue" simply means GMV.
  - Answers are stored on the `Session` under stable keys (`METRIC_DEFINITION:success_rate`). After two clarification turns the defaults are used and disclosed.
- **Planning** ([`analyst/planner.py`](../analyst/planner.py)) is deterministic.
  - Each question type has a template: driver analysis, trend, ranking, comparison, lookup or reconciliation.
  - Every query step is a `QuerySpec` that the semantic compiler turns into exact metric SQL.
  - Driver analyses get both periods for every candidate dimension, with volume columns for the decomposition, inside a query budget. Dimensions a filter already fixes are skipped.
  - The analysis engine then adds its own follow-up queries (drill-downs, failure codes, reconciliation tests), each through the same gate.
  - A result-size guard estimates the output rows (e.g. day × hour × merchant over a year is about 1.1M) and offers concrete narrowing options instead of running.
- **SQL generation and repair** ([`analyst/sqlgen.py`](../analyst/sqlgen.py)). The model writes SQL only for steps the metric layer can't express (`custom_request`). It runs on the reasoning-tier model of whichever backend is configured, with the role-filtered schema and the canonical metric definitions in the prompt.
  - Whatever it writes goes through the SQL safety gate.
  - Repairable failures and execution errors go back to the model with the exact error, at most 2 times.
  - Unsafe SQL, access denials and too-expensive queries are final and never re-prompted.
- **Follow-ups** ("Drill into UPI Collect by merchant") keep the previous metric, periods and filters.
- **Trace.** Every stage appends `{stage, status, ms, tokens}` to `trace`, for the steps panel and the `run_steps` table later.

### LLM backends

[`analyst/llm.py`](../analyst/llm.py) has one interface, `llm.json(task, system, user, schema, tier)`, with these implementations:

| Backend | When | Notes |
|---|---|---|
| `AnthropicLLM` | `llm.provider: auto` with `ANTHROPIC_API_KEY` (or an `ant auth login` profile) set, or `--llm anthropic` | Official `anthropic` SDK with structured outputs (`output_config.format`). The stable system prompt is cached (`cache_control`). Effort is `low` for parsing and `medium` for SQL. Opus calls opt into the server-side refusal fallback (`fallbacks: "default"`). Refusals and `max_tokens` cut-offs raise, so the pipeline fails gracefully. |
| `GeminiLLM` | `GEMINI_API_KEY` (Google AI Studio) set and no Anthropic credentials, or `--llm gemini` | Gemini REST API through `httpx` (no extra SDK) with `responseJsonSchema` structured output. Retries 429/5xx with the server's suggested delay; a used-up daily quota fails at once. Models in `llm.gemini` (`gemini-3.1-flash-lite` for parsing, `gemini-3.5-flash` for SQL, wording and the eval judge). Thinking tokens count as output. |
| `OpenRouterLLM` | `OPENROUTER_API_KEY` set (after Anthropic and Gemini), or `ANALYST_LLM_PROVIDER=openrouter`, or `--llm openrouter` | OpenAI-compatible REST through `httpx`, with `response_format: json_schema` and `require_parameters` routing. `llm.openrouter.models_*` lists free models in order, so OpenRouter falls through to the next when one is rate-limited. **Hard call cap** per process: `max_calls` / `OPENROUTER_MAX_CALLS`; every request counts, retries included. |
| `FallbackLLM` | wraps any remote backend when `llm.fallback_to_offline: true` | A failed call (rate limit, used-up free quota, outage) is answered by the offline rules for that task, so a live demo keeps working. The trace shows `offline-rules` as the model, and the answer reads "worded by offline (fallback)". The eval harness switches it off, so evals measure the model itself. |
| `OfflineLLM` | no credentials (the default here) | Deterministic rule-based parser in [`analyst/intent.py`](../analyst/intent.py). It reads the structured context rather than the prompt and covers the demo and eval questions. It cannot write custom SQL and says so. |
| `ScriptedLLM` | tests | Replays canned replies (including invalid JSON and dangerous SQL) to drive retries, the repair loop and the gate. |

Configuration lives in `config/analyst.yaml` under `llm:`, `defaults:` and `ambiguity:`. `scripts/check_pipeline.py --llm anthropic` (or `--llm gemini`) runs the same eval checks through a real model.

**The model's parse is checked against the question's own words.** Testing with Gemini showed a real model fills in things the user never said. [`normalize`](../analyst/intent.py) now corrects them deterministically, whatever the model, and notes each change in the trace:

| What the model did | What happens now |
|---|---|
| Picked the default success-rate definition itself, which skipped the clarification | A definition counts as chosen only if the question names it ("order-level", "technical", "all attempts"…) |
| Added breakdowns or a daily grain nobody asked for | Kept only if the question mentions them ("by issuer", "hourly", "weekly"…) |
| Answered "why …" or "what happened …" as a trend or ranking | Treated as a driver analysis, with a previous-period comparison |
| Read "X by Y for a period" as a trend | Treated as a one-period breakdown |
| Returned a reconciliation with no metrics and a free-form custom query instead | Both sources are filled in; a custom query never replaces an analysis template |
| Dropped "NORTH region" for a WEST-scoped user, because the value isn't in their catalogue | The value is kept, so the access check refuses it, instead of quietly answering for WEST |

## Analysis engine


[`analyst/analysis.py`](../analyst/analysis.py) turns executed results into findings. Every extra query it needs (drill-downs, failure codes, reconciliation tests) runs **through the same gate as the user**, with row-level security, and shows up as an extra step.

| Method | What it does |
|---|---|
| Headline | P0 / P1 / delta. Rates carry their numerator and denominator (`success_rate#num` / `#den`, a new semantic-layer feature), so changes get a two-proportion z-test. |
| Mix / rate decomposition | Exact and symmetric (residual 0 by construction). Per-segment mix effects are **centred on the average rate**, (w₁−w₀)·(r̄ᵢ−R̄). Totals are identical to the SPEC formula, but each segment's number now reads on its own: a growing, below-average segment shows a negative mix effect instead of a large positive one. New or disappearing segments are treated as pure mix. Segments get a significance test and a low-volume flag. |
| Contribution | Additive metrics (GMV, counts): segment deltas and share of the change. |
| Best dimension + driver ranking | Each split is scored by **how well it explains the change**. A mix-dominant split explains it away (every segment's own rate is flat), so it scores by its mix share. Otherwise it scores by concentration: share of the change minus share of volume, so a big segment absorbing a broad drop explains little. A child dimension that explains as well as its parent wins (sub_method over payment_method). Drivers are ranked by lift × significance × volume weight. |
| Drill-down (≤ 3 levels) | Applied when the top driver explains more than 50% of the change. **Mix-driven:** shows where the extra volume came from, by merchant. **Rate-driven:** looks at the next dimensions, the affected hours (for single days) and the failure codes that rose. |
| Data quality ([`analyst/dq.py`](../analyst/dq.py)) | DQ01 duplicates (mitigated by the dedup view), DQ02 null keys (scoped to the dimensions they affect), DQ03 orphan keys, DQ05 late-arriving days, DQ06 volume anomalies (robust z-score, calendar events named), DQ07 value ranges, DQ08 status validity, DQ09 source mismatch, DQ10 merchant reclassification. Severity is INFO, WARN or BLOCK. A BLOCK on an affected analysis gives `ANSWERED_WITH_DQ_BLOCK`. |
| Reconciliation | Dashboard GMV vs the finance ledger: a waterfall of PENDING / REVERSED / FAILED / ON_HOLD settlements, payments with no settlement record, the UTC vs IST day boundary, and merchants missing from the ledger. It ends with the residual, a unit (paise/rupee) check and a recommendation. |
| Hypotheses → recommendations | Templated from patterns: mix shift, issuer outage (hours + code 91/96), acquirer degradation (timeouts), rate change in a segment, incomplete data, reclassification, sale events, an unsupported premise ("why did it drop?" when it didn't), and a volume shift. Each recommendation cites its hypothesis and, where computable, an expected impact in pp. |

What it finds on the planted anomalies (all asserted in `scripts/check_pipeline.py`):

| Question | Result |
|---|---|
| Why did success rate drop in the last two weeks? (A1) | 88.13% → 85.49%. **MIX_DOMINANT** by sub_method: mix −2.54pp, rate −0.09pp, matching ground truth exactly. UPI_COLLECT went from 12.6% to 32.8% of volume while its own rate stayed flat at about 77%. 97% of the extra collect volume is **QuickBazaar**. Recommendation: move QuickBazaar's collect traffic to intent (about +1.97pp). |
| Drill into UPI Collect by merchant (follow-up) | Rate change not significant (p = 0.40). Volume shifted to QuickBazaar (0% → 72% of the segment). |
| What happened to card success on September 10? (A2) | **Meridian Bank** issuer outage, **18:00–23:00 IST**, failures dominated by code **91**. |
| Worst card-not-present acquirer in late September? (A3) | **Acquirer Beta**: acquirer-timeout failures rose from 1.9% to 5.7% of attempts. Recommendation: shift routing. |
| Why does finance's October settled amount differ? (A6) | 3.53% gap fully explained to the paisa (residual ₹0): reversed ₹73.1L, missing merchants ₹59.2L, failed ₹51.1L, on hold ₹30.9L, timezone ₹4.9L. |
| Did success rate drop yesterday? (A7) | −0.20pp, not significant, so "premise not supported". The incomplete Oct 28 is excluded and flagged. |

## Answer composer


[`analyst/composer.py`](../analyst/composer.py) produces the SPEC 15.1 answer for every question. Waiting, refusal and direct answers get a minimal answer, with options or alternatives as follow-ups.

1. **Deterministic draft.** It contains:
   - a headline written for the question type, and the premise corrected when the data contradicts it ("No — success rate did not meaningfully drop…");
   - facts, each citing the query steps it came from;
   - hypotheses with a confidence level and a "how to validate" step;
   - recommendations that reference them, with an impact estimate, effort and an owner hint;
   - caveats: data-quality issues, assumed interpretations ("I assumed …"), and narrowing or as-of notes;
   - definitions: metric, IST time window, row-level scope and data version;
   - an evidence list (SQL actually executed, row-level-security predicates, data version) and suggested follow-ups.

   Every number is formatted from a computed value, so the draft is grounded by construction.
2. **Optional LLM wording** (`compose` task, reasoning tier). The model may rewrite the text but must keep the same ids and structure.
3. **Number grounding.** Every number in the headline, facts, hypotheses and recommendations is extracted and matched against the values the queries and the analysis produced.
   - Tolerance matches the displayed precision. %, pp, ₹ Cr / L, and sign written separately are all handled. Dates, times, ids and small counts are ignored.
   - A rewrite with an untraceable number goes back to the model with the offending tokens, at most 2 times.
   - After that, the verified draft wording is used, confidence drops by 0.3, and the answer says the wording could not be verified.
4. **Confidence** (SPEC 15.3). Starts at 1.0 and is adjusted for:
   - sample size and significance;
   - data-quality severity: a warning that hits the analysed dimension by more than 5% costs 0.3, which is why the Oct 18–20 segment breakdown comes out MEDIUM;
   - assumed interpretations and the clarification limit;
   - mixed decompositions, low-volume drivers, SQL repairs and failed steps.

   HIGH ≥ 0.75, MEDIUM ≥ 0.5, otherwise LOW, which gives `ANSWERED_LOW_CONFIDENCE`. The top reasons and limitations are listed.
5. **Charts** (SPEC 16), as declarative specs with inline data:
   - KPI tiles;
   - a trend line with P0/P1 bands and anomaly or sale-event annotations, plus a late-data badge;
   - a mix/rate waterfall (P0 → mix → rate → P1, summing exactly) and share-of-volume bars;
   - hourly bars for an outage;
   - a reconciliation waterfall (dashboard → each factor → finance ledger);
   - horizontal bars for breakdowns and a table for rankings.

`scripts/ask.py` prints the composed answer:

```
Success rate fell 1.74pp (87.3% → 85.6%) in Sep 21–30 vs Sep 11–20; Acquirer Beta degraded, with acquirer timeouts driving most of the drop.
- Acquirer Beta (acquirer): share of volume 34.8% → 34.9%, own rate 86.6% → 82.8%; it accounts for -1.30pp (74% of the change).
- Top rising failure: Acquirer response timeout (code ACQ_TO), 1.9% → 5.7% of attempts.
Recommendation: Shift routing away from Acquirer Beta until its timeouts return to normal. Expected impact: +1.30pp overall.
Confidence: HIGH · Grounding: 20 numbers checked, all traced to query results
```

The eval checks now cover each case's answer-level expectations:
- **must-mention:** "mix" and "UPI Collect" for EV01; "18:00" and "23:00" for EV02; "timeout" for EV14;
- **must-not-claim:** "UPI Collect success rate declined" and "dropped significantly";
- **required caveats:** DQ01 for EV07 and DQ02 for EV13;
- **scope disclosure:** EV04;
- **on every answer:** grounding and the composition rules.

## API and metadata DB


`uvicorn analyst.api:app` serves the SPEC section 20 API ([`analyst/api.py`](../analyst/api.py)). The service layer is [`analyst/service.py`](../analyst/service.py), and the metadata DB is [`analyst/store.py`](../analyst/store.py).

| Method | Path | |
|---|---|---|
| POST | `/api/v1/sessions` | Start a session |
| POST | `/api/v1/sessions/{sid}/questions` | Ask. Returns 202 with `question_id` and `stream_url`; `?wait=true` runs synchronously. |
| GET | `/api/v1/questions/{qid}/stream` | **SSE**: `run_started`, `stage_started`, `stage_completed`, `clarification_needed`, `narrowing_needed`, `answer`, `done`. Resumes with `Last-Event-ID` or `?after=`. |
| POST | `/api/v1/questions/{qid}/clarify` / `narrow` | Answer a clarification (`{choices: {key: option}}`) or pick a narrowing option. Continues the same question. |
| GET | `/api/v1/questions/{qid}` and `/steps` | Full result (answer, analysis, plan) with every executed query, and the stage trace |
| POST | `/api/v1/queries/{id}/rerun` | Re-execute a stored query **through the full gate again** as the current user, bypassing the cache |
| POST | `/api/v1/questions/{qid}/feedback` | Rating |
| GET | `/api/v1/catalog`, `/dq/summary?from=&to=`, `/audit`, `/me`, `/users`, `/info`, `/health` | Role-filtered catalog, data-quality dashboard, access audit (yours; everyone's for admins) |
| POST/GET/PUT | `/api/v1/evals/run`, `/admin/config` | Admin only. Runs the eval checks (stored in `eval_runs`), edits whitelisted runtime settings. |

Authentication is mocked, as the spec allows: the caller identifies with `X-User-Id`.
- That header only selects who is asking. Policies and row-level security are applied again inside every run.
- Another user's session, question or query returns **404**, so ids can't be probed.
- `u_exec` carries `admin: true`.

**Metadata DB.** SQLite at `data/app.sqlite`, with the SPEC section 19 schema. It stores:
- sessions, with conversation context (clarifications, recent intents) so follow-ups survive a restart;
- questions, `run_steps` (every stage, with latency and tokens) and `queries` (generated SQL, final SQL after the RLS rewrite, validation, RLS predicates, cache hit, data version, result sample);
- clarifications and the user's choices, answers, feedback;
- the `access_audit` table (QUERY_EXECUTED, ACCESS_DENIED, UNSAFE_REJECTED);
- `query_cache`, `dq_results`, and eval runs.

**Result cache** (SPEC 17). The key is `sha256(final SQL including RLS literals + role + data version)`.
- Validation always runs first, so a cached result is only returned for a query that passes the gate right now.
- A data reload changes the version, so stale results are never served.
- A different scope produces different SQL, so one user is never served another user's result.
- A repeated question is served entirely from cache (19 of 19 queries for the A1 driver analysis).

**Concurrency.** Runs execute on a worker pool, one at a time per session (they share conversation state). A question can have several runs (ask, clarify, narrow); its stream replays all of them and ends at the last `done`.

```bash
SID=$(curl -s -X POST localhost:8000/api/v1/sessions -H "X-User-Id: u_exec" | jq -r .session_id)
QID=$(curl -s -X POST localhost:8000/api/v1/sessions/$SID/questions -H "X-User-Id: u_exec" \
       -H "Content-Type: application/json" -d '{"text":"Why did our payment success rate drop in the last two weeks?"}' | jq -r .question_id)
curl -N localhost:8000/api/v1/questions/$QID/stream -H "X-User-Id: u_exec"          # ... clarification_needed
curl -s -X POST localhost:8000/api/v1/questions/$QID/clarify -H "X-User-Id: u_exec" \
       -H "Content-Type: application/json" -d '{"choices":{"METRIC_DEFINITION:success_rate":"a"}}'
curl -s localhost:8000/api/v1/questions/$QID -H "X-User-Id: u_exec" | jq .result.answer.headline
```

## UI


`uvicorn analyst.api:app --port 8000`, then open http://localhost:8000. The page lives in [`analyst/web/`](../analyst/web/): one HTML file, one stylesheet and one script, with no build step, no npm and no CDN, so it works offline. It uses only the public API above.

The SPEC section 20 panels:
- **Role switcher.** Pick any seeded user (mock sign-in; the choice is sent as `X-User-Id`). Each switch starts a new conversation. The sidebar shows example questions for that role, the metrics and breakdowns the role may use, its row scope, and what it cannot see.
- **DQ banner.** Data-quality issues for the current window (from `/dq/summary`), expandable.
- **Chat with live progress.** Stages appear as chips while the run streams over SSE. The stream is read with `fetch()`, because `EventSource` cannot send the user header; it resumes with `?after=`.
- **Clarification and narrowing.** Each definition is shown with its value for your period and the default preselected. Answering continues the same question.
- **Answer card.**
  - Headline, then badges: confidence, how many numbers were checked against query results, query count (and whether results came from the cache), and row scope.
  - Facts, hypotheses and recommendations, colour-coded. Each fact's evidence ids jump to the query behind it.
  - Caveats, confidence reasons, and the definitions used (metric, period, scope, data version).
- **Charts.** The composer's chart specs (SPEC 16), drawn as inline SVG: KPI, daily line with period bands and marked days, mix/rate waterfall, grouped bars (with the outage-hour band), horizontal bars, ranking table.
- **Evidence.** For every query:
  - the final SQL, with a copy button;
  - the SQL before the access rules were applied;
  - the access rules applied (for example `region = 'WEST'`);
  - the result rows, data version and cache status;
  - **Re-run**, which executes the query again through the full safety gate as the current user.
- **Steps timeline.** Every stage with its status, detail and latency, and token counts.
- Follow-up suggestions as one-click chips, and thumbs-up/down feedback.

Every demo path in SPEC section 22 was walked through in the browser:
- the driver question with its clarification (mix shift towards UPI_COLLECT, QuickBazaar);
- the card outage on Sep 10 (Meridian Bank, 18:00-23:00, with the hour chart);
- the ops analyst refused MDR revenue;
- the delete request rejected;
- the West manager's ranking with the injected region rule shown in evidence;
- the finance reconciliation waterfall;
- the trend question.

## Evaluation


[`evals/cases.yaml`](../evals/cases.yaml) holds the SPEC section 18 cases EV01–EV15, plus five robustness cases (EX16–EX20: out-of-scope region, entity outside scope, unknown metric, forecast, merchant-user scope). They are adapted to the generated data: Meridian Bank on Sep 10, Acquirer Beta, and the mix shift reported by sub_method.

The harness ([`analyst/evals.py`](../analyst/evals.py)) runs each case through the real pipeline as the case's user. A clarification is answered as the case specifies; the run stops at the expected waiting state when that is the assertion. Each case is graded by up to four graders:

| Grader | Checks |
|---|---|
| numeric | headline P0/P1, decomposition totals, counts, ranked rows, segment breakdown incl. the unknown bucket, reconciliation factors, outage window, failure code; all against `fixtures/ground_truth.json` |
| structural | status, ambiguity asked, caveat codes, top driver, interpretation, required and forbidden phrases, scope disclosed, RLS predicate in every executed query, no queries for refusals, and **what actually reached the executor** (the gate's execute and EXPLAIN calls are recorded; no DROP / DELETE / ATTACH / COPY) |
| sql_equivalence | a hand-written reference query, run directly on the warehouse, returns the same result set as the step the pipeline ran (order-insensitive) |
| llm_judge | rubric scores (faithfulness, fact/hypothesis separation, clarity ≥ 4/5) from a model that sees the evidence rows. Skipped with the offline backend. |

A case passes when every grader that ran passed. The report gives pass rate per category and per grader, p50/p95 latency, tokens and cost (from `llm.pricing` in `config/analyst.yaml`), and the 90% gate on EV01–EV15. Runs are stored in `eval_runs` / `eval_results`.

```bash
python scripts/run_evals.py                   # offline; writes evals/REPORT.md and evals/last_run.json
python scripts/run_evals.py --case EV01 EV08  # a subset
python scripts/run_evals.py --llm anthropic   # through Claude, with the LLM judge
python scripts/run_evals.py --llm gemini --out evals/gemini   # through Gemini (GEMINI_API_KEY)
```

Admins can also run the suite from the UI sidebar or with `POST /api/v1/evals/run` (`{"cases": [...]}` optional).

[`tests/test_evals.py`](../tests/test_evals.py) checks that the graders **fail** wrong answers: a changed expected value, the wrong status, a reference query over the raw (duplicated) table, a simulated DELETE reaching the executor, and a low judge score each flip the grade.

**Results** (full warehouse):

| Backend | Cases | EV01–EV15 | LLM judge | Report |
|---|---|---|---|---|
| offline (rules) | 20/20 | 15/15 | skipped | [evals/REPORT.md](../evals/REPORT.md) |
| Gemini, first run | 13/20 | 9/15 (gate not met) | 0/3 passed | [evals/gemini/run1-REPORT.md](../evals/gemini/run1-REPORT.md) |
| Gemini, after the fixes | 19/20 | 14/15 (gate met) | 2/2 passed (3 skipped: daily quota) | [evals/gemini/REPORT.md](../evals/gemini/REPORT.md) |

The first Gemini run exposed the parse problems in the table under [LLM backends](#llm-backends). It also exposed a harness flaw: the judge saw only the first 20 rows of each query, not the computed analysis behind the facts, so it marked correct numbers as unsupported. Both were fixed, and every fix has a regression test ([`tests/test_gemini.py`](../tests/test_gemini.py)). The offline run is unchanged at 20/20.

**EV02 was flaky on Gemini; now it isn't.** The final Gemini run's failure varied between runs. Feeding `normalize` every plausible reading of the EV02 question (270 combinations of question type, compare, grain and breakdowns) showed two the guards missed. A COMPARISON or DISTRIBUTION reading of "What happened to…" left no comparison period, which leaves only a KPI tile, exactly the failure seen. Both are now treated as driver analyses unless the question really compares segments ("UPI vs cards"). An invented comparison phrase is ignored. All 270 readings now give the same analysis, and an end-to-end test runs the pipeline under six of them ([`tests/test_gemini.py`](../tests/test_gemini.py)). Gemini's quota was used up, so the full Gemini suite has not been rerun after this fix.

Before that fix, the remaining Gemini failure varied between runs. A run before the final one failed only EV13 (fixed since). The final run failed only EV02, which then passed three times out of three when repeated. The model's parse is not deterministic, and the guards don't yet cover whatever reading it produced that time. Each eval result now records the parsed intent, and the report prints it for every failure, so the next variation can be diagnosed. Gemini ran on the free tier, so cost is $0 and latency includes waits for rate limits. The reasoning model moved from `gemini-3-flash-preview` to `gemini-3.5-flash` after the first run, because the preview model's daily free quota ran out. Claude has not been evaluated: the Anthropic key available had no API credits.

These cases test that the system finds the planted answers end to end. They are not a measure of generalisation to new phrasings; see the [write-up](WRITEUP.md#evaluation).
