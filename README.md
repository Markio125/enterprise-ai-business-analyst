# Enterprise AI Business Analyst

An AI analyst for payments data. Ask a business question in plain English, for example *"Why did our payment success rate drop in the last two weeks?"*. It works out which data and metrics to use, writes and validates SQL, runs it read-only and analyses the result. The answer comes with charts, the exact queries behind every number, and a clear split between facts, hypotheses and recommendations.

The design rule is **the language model proposes, code decides.** The model reads the question and words the answer. Code decides what you are allowed to see, whether a query is safe to run, and every number. Each number in an answer is checked against the query results before it is shown.

**What it handles**
- **Mix shift vs real decline:** a metric change is split exactly into "the traffic mix changed" and "performance changed", so a shift toward a weaker channel isn't mistaken for a failure.
- **Ambiguous questions:** when "success rate" or "lately" could mean several things, it asks, and shows what each option gives.
- **Access control:** restricted metrics are refused with alternatives, and row-level scope (e.g. `region = 'WEST'`) is injected into every query.
- **Unsafe requests:** data changes and prompt injection are rejected. Only validated, read-only SQL ever runs.
- **Data quality:** duplicates, missing keys and late-arriving data are detected and stated as caveats.
- **Sources that disagree:** dashboard vs finance figures are reconciled factor by factor.
- **Expensive questions:** you are asked to narrow them, instead of the system running a million-row query.

**Documentation**
- [Design write-up](docs/WRITEUP.md): the decisions, how each failure scenario is handled, results and limitations.
- [Architecture](docs/ARCHITECTURE.md): diagrams of the components, question lifecycle and access layers.
- [Technical reference](docs/DETAILS.md): the data, semantic layer, SQL safety, pipeline, analysis, API and UI in depth.
- [Demo walkthrough](docs/DEMO.md), plus a [3½-minute talk script](docs/DEMO_TALK.md).
- [Original specification](docs/SPEC.md).

## Run it on your machine

**You need:** Python 3.11 or newer, Git, and about 1 GB of free disk. **No API key is needed**: without one, the app uses its built-in rule-based backend, so every feature in the demo works offline.

### Windows (PowerShell)

```powershell
git clone https://github.com/Markio125/enterprise-ai-business-analyst.git
cd enterprise-ai-business-analyst
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python scripts/generate_data.py
.venv\Scripts\python -m uvicorn analyst.api:app --port 8000
```

### macOS / Linux

```bash
git clone https://github.com/Markio125/enterprise-ai-business-analyst.git
cd enterprise-ai-business-analyst
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/generate_data.py
.venv/bin/python -m uvicorn analyst.api:app --port 8000
```

What each step does:
1. **Install** (`pip install`): a few minutes, depending on your connection.
2. **Build the data** (`generate_data.py`): about 1M synthetic payment attempts in `data/warehouse.duckdb`, in under a minute. The data is deterministic, so everyone gets the same numbers. You only do this once.
3. **Start the server** (`uvicorn`): wait for `Uvicorn running on http://127.0.0.1:8000`.

Then open **http://localhost:8000** in a browser:
- Pick a user at the top right; each one has a different role and access.
- Click an example question in the sidebar, or type your own. *"Why did our payment success rate drop in the last two weeks?"* is the main demo.
- [docs/DEMO.md](docs/DEMO.md) walks through every scenario. The API docs are at http://localhost:8000/docs.

Stop the server with `Ctrl+C`.

### Optional: use a real language model

Copy `.env.example` to `.env` (it's gitignored, so it's never committed) and fill in **one** key:
- `ANTHROPIC_API_KEY` (Claude);
- `GEMINI_API_KEY` (Google AI Studio);
- `OPENROUTER_API_KEY` (OpenRouter, which has free models).

Then restart the server. The sidebar shows which model is in use. The safety checks, the analysis and the numbers are the same with or without a model; the model only improves how questions are understood and how answers are worded. If a free model hits its rate limit, that step falls back to the built-in rules, so the app keeps working.

### Troubleshooting

| Problem | Fix |
|---|---|
| `python` not found, or the version is below 3.11 | Install Python from python.org. On Windows, `py -3.12 -m venv .venv` picks a specific version |
| Port 8000 already in use | Add `--port 8001` and open http://localhost:8001 |
| The page says it can't reach the API, or the server errors about the warehouse | Run `generate_data.py` first; the server needs `data/warehouse.duckdb` |
| You'd rather activate the venv | Windows: `.venv\Scripts\Activate.ps1` (if blocked: `Set-ExecutionPolicy -Scope Process Bypass`). macOS/Linux: `source .venv/bin/activate`. Then plain `python` works |

To check everything: `python -m pytest -q` runs the full test suite in about 3 minutes, and `python scripts/run_evals.py` runs the known-answer evaluation.

## Evaluation

The synthetic data has planted anomalies with known answers ([`fixtures/ground_truth.json`](fixtures/ground_truth.json)). [`evals/cases.yaml`](evals/cases.yaml) has 20 questions about them. The harness ([`analyst/evals.py`](analyst/evals.py)) runs each one through the real pipeline, as the right user, and grades it four ways:
- **numeric:** the numbers against the ground truth;
- **structural:** status, caveats, top driver, access rules applied, and nothing unsafe reaching the database;
- **SQL equivalence:** the result must match hand-written reference SQL;
- **LLM judge:** faithfulness to the evidence, fact/hypothesis separation, and clarity.

| Backend | Cases | Core cases EV01–EV15 (gate ≥ 90%) | Report |
|---|---|---|---|
| Built-in rules (offline) | 20/20 | 15/15 | [evals/REPORT.md](evals/REPORT.md) |
| Gemini, first run | 13/20 | 9/15 | [evals/gemini/run1-REPORT.md](evals/gemini/run1-REPORT.md) |
| Gemini, after fixes | 19/20 | 14/15 | [evals/gemini/REPORT.md](evals/gemini/REPORT.md) |

Testing on a real model's output found real problems. The model filled in a metric definition the user never chose, added breakdowns nobody asked for, and dropped an out-of-scope filter. Each is now corrected by deterministic checks against the question's own words, with regression tests. The cases test that known answers are found end to end; they don't measure how the system generalises to new phrasings. Details are in [docs/DETAILS.md](docs/DETAILS.md#evaluation) and the [write-up](docs/WRITEUP.md#evaluation).

```bash
python scripts/run_evals.py                  # offline -> evals/REPORT.md
python scripts/run_evals.py --llm openrouter # through a real model (key in .env)
```

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
  llm.py            LLM backends: Claude, Gemini, OpenRouter, offline rules; call cap, offline fallback
  intent.py         intent schema + prompt, normalisation against the semantic layer, offline parser
  ambiguity.py      ambiguity rules, materiality probes, in-scope merchant resolution
  planner.py        question-type plan templates, result-size guard / narrowing
  sqlgen.py         LLM SQL generation + repair loop through the safety gate
  pipeline.py       the state machine (Analyst.ask), Session, trace
  analysis.py       decomposition, driver ranking, drill-down, trend, reconciliation, hypotheses
  dq.py             data-quality checks DQ01-DQ10 (cached per window and data version)
  composer.py       answer draft, LLM wording, number grounding, confidence, chart specs
  store.py          SQLite metadata DB (SPEC 19) + query-result cache
  service.py        sessions, background runs, event streams, ownership checks, admin
  api.py            FastAPI app (SPEC 20) with SSE; serves the UI at /
  web/              the UI: index.html, app.css, app.js (no build step)
  evals.py          eval harness: case runner, graders, cost, report (SPEC 18)
semantic/model.yaml     entities, measures, metrics, dimensions, ambiguous terms, glossary
config/generator.yaml   every generator knob (volumes, rates, anomaly windows and magnitudes)
config/policies.yaml    roles, row-level scopes, demo users
config/analyst.yaml     warehouse limits, cost controls, SQL-safety lists
scripts/            generate_data.py, verify_data.py, check_semantic.py, check_safety.py, check_pipeline.py,
                    catalog.py, query.py, run_sql.py, ask.py, run_evals.py, demo.py
evals/              cases.yaml (the eval questions) and REPORT.md files (latest results)
fixtures/ground_truth.json
tests/
docs/WRITEUP.md     design write-up
docs/ARCHITECTURE.md diagrams (components, lifecycle, access layers, number provenance)
docs/DETAILS.md     technical reference for each component
docs/DEMO.md        demo walkthrough; DEMO_TALK.md is a 3½-minute talk script
docs/SPEC.md        original system specification
.env.example        template for optional model keys (copy to .env)
```

## Developer commands

With the virtual environment active (see [Troubleshooting](#troubleshooting)):

```bash
python scripts/generate_data.py   # builds data/warehouse.duckdb (~1.05M attempts)
python scripts/verify_data.py     # checks every planted anomaly, writes fixtures/ground_truth.json
python scripts/check_semantic.py  # validates the semantic layer + policies, re-derives the ground truth through it
python scripts/check_safety.py    # ground truth through the SQL gate + an attack catalog (34 checks)
python scripts/check_pipeline.py  # the SPEC eval questions end to end (pipeline, analysis, answer), vs ground truth (53 checks)
python scripts/run_evals.py       # the eval suite (evals/cases.yaml) -> evals/REPORT.md
python scripts/demo.py            # the demo script, in the terminal
pytest -q                         # all tests (fast ones on a reduced-scale warehouse, plus full-warehouse checks)
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

# ask questions in plain English; -i keeps a session for clarifications and follow-ups
python scripts/ask.py -i --user u_exec
python scripts/ask.py --user u_exec --answer METRIC_DEFINITION:success_rate=a "Why did our payment success rate drop in the last two weeks?"
python scripts/ask.py --user u_west "Top 5 merchants by GMV last month"

# the UI and the HTTP API: open http://localhost:8000 ; API docs at http://localhost:8000/docs
uvicorn analyst.api:app --port 8000
# to use a real model, copy .env.example to .env and fill in one key (see "Run it on your machine")
```

`python scripts/generate_data.py --scale 0.2` builds a smaller warehouse for quick iteration. Output is deterministic for a given seed and config.

## Mocked dependencies

| Real world | Here |
|---|---|
| Enterprise warehouse (Snowflake / BigQuery) | DuckDB file, opened read-only |
| Identity provider / SSO | Seeded users in `config/policies.yaml`; `X-User-Id` header; user picker in the UI |
| Data catalogue | YAML semantic layer (`semantic/model.yaml`) |
| Finance ledger system | `finance_daily_settlement_summary`, an independent aggregate that disagrees on purpose |
| Live data | Synthetic, seeded, deterministic |
| LLM provider (when no key) | Offline rule-based backend for parsing, SQL and wording |

## Assumptions & limitations

- Synthetic data only. Rates and distributions are plausible but not calibrated to any real processor.
- Anomalies are isolated in time so each has one clean, verifiable explanation.
- Every `v_attempts` query scans the whole raw table: the view's deduplication window blocks filter pushdown. That is fine at 1M rows. At scale, the deduplicated table should be materialised so time filters cut the scan, and the cost guard would show the difference.
- The time-filter rule (S07) is best-effort: any predicate on a fact's time column counts. The EXPLAIN cost guard (S12) is the backstop.
- Data-quality checks are fixed system queries over the whole warehouse (not scoped by role). They report only rates and dates, never values or row counts.
- Hypotheses are templates over measured patterns. They are not causal proof; answers label them as hypotheses, with confidence and a validation step.
- The UI is a demo client, not a hardened product. There is no real sign-in (anyone who can reach the server can pick any user), and charts are hand-drawn SVG rather than a charting library, to keep it dependency-free.
- Mock authentication: `X-User-Id` must be replaced by real identity (SSO/JWT) before any deployment. Event streams are kept in process memory (replayed from the metadata DB after a restart), so the API runs as a single process; several processes would need a shared event bus.
- Grounding checks that each number traces to a computed value. It does not check that the surrounding claim is right (e.g. a rewrite that attaches a correct number to the wrong segment). Keeping the facts' structure fixed and only letting the model reword limits that risk; the eval checks also assert key claims.
- OpenRouter was tested live with 6 calls (free models): the main demo question with its clarification, and EV02, both correct and both worded by the model under the grounding check. The full eval suite was not run on OpenRouter, to save the free quota for demos. Free models have per-minute and daily request limits; when they hit, the offline fallback answers instead.
- The Gemini runs used the free tier: rate-limit waits inflate latency, daily quotas cut the judge short, and cost shows $0 because no Gemini prices are configured.
- The eval cases come from the same planted anomalies the analysis was built around. They show the system finds known answers end to end, not that it generalises; a held-out question set is the next step.
- The offline parser is rules, tuned on the demo and eval questions; real phrasing variety needs a model backend. Gemini and OpenRouter were tested live. Claude was not (no API credits were available), though its request shape is unit-tested against a fake client.
- Merchant-name entity resolution is exact, then unique substring, then a fuzzy "did you mean". Names are not unique across regions; resolution only ever sees the user's scope.
- The generator favours clarity over realism in places. For example, merchant success-rate noise is centred so the A1 campaign merchant is exactly average, which makes A1 a near-pure mix effect.
