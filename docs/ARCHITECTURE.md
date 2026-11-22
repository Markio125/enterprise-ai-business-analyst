# Architecture

The design rule throughout: **the language model proposes, deterministic code decides.** The model reads the question, writes SQL and words the answer. Code decides what may run (policies, SQL validation, row-level security, cost limits), computes every number (decomposition, driver ranking, reconciliation), and checks that every number in the answer traces back to a query result.

## Components

```mermaid
flowchart LR
  subgraph Client
    UI["Web UI<br/>analyst/web<br/>chat · answer card · charts · evidence"]
    CLI["scripts/ask.py<br/>scripts/run_evals.py<br/>scripts/demo.py"]
  end

  subgraph API["FastAPI · analyst/api.py"]
    R["REST /api/v1<br/>sessions · questions · clarify · narrow<br/>rerun · catalog · dq · audit · evals"]
    SSE["SSE stream<br/>stage events, resumable"]
  end

  SVC["AnalystService<br/>analyst/service.py<br/>worker pool · ownership checks · event log"]
  P["Pipeline state machine<br/>analyst/pipeline.py"]

  subgraph Brain["Language model (optional)"]
    LLM["Claude via anthropic SDK<br/>Haiku 5.5: parse · Opus 5.5: SQL, wording<br/>or the offline rule-based backend"]
  end

  subgraph Deterministic["Deterministic core"]
    SEM["Semantic layer<br/>semantic/model.yaml<br/>metrics · dimensions · synonyms"]
    POL["Policies<br/>config/policies.yaml<br/>roles · row scopes"]
    GATE["SQL safety gate<br/>analyst/sqlsafety.py<br/>sqlglot AST rules S01–S16 · RLS rewrite · EXPLAIN cost guard"]
    AN["Analysis engine<br/>analyst/analysis.py<br/>mix/rate decomposition · drivers · drill-down · reconciliation"]
    DQ["Data-quality checks<br/>analyst/dq.py · DQ01–DQ10"]
    COMP["Composer<br/>analyst/composer.py<br/>facts / hypotheses / recommendations · number grounding · confidence · chart specs"]
  end

  WH[("Warehouse<br/>DuckDB, opened read-only<br/>star schema + finance ledger")]
  META[("Metadata DB · SQLite<br/>sessions · runs · queries · audit<br/>cache · feedback · eval runs")]

  UI --> R
  UI --> SSE
  CLI --> P
  R --> SVC
  SSE --> SVC
  SVC --> P
  SVC --> META
  P --> LLM
  P --> SEM
  P --> POL
  P --> GATE
  P --> AN
  P --> DQ
  P --> COMP
  GATE --> WH
  DQ --> WH
  GATE -. result cache .-> META
```

## The question lifecycle

Every question runs through one state machine ([`analyst/pipeline.py`](../analyst/pipeline.py)). Each stage appends to a trace, and the trace is streamed to the UI as it happens.

```mermaid
stateDiagram-v2
  [*] --> PARSING
  PARSING --> REJECTED_UNSAFE: asks to change data / injection
  PARSING --> ANSWERED_DIRECT: definition, unknown metric, forecast, off-topic
  PARSING --> ACCESS_PRECHECK
  ACCESS_PRECHECK --> REFUSED_ACCESS: restricted metric, table or row scope
  ACCESS_PRECHECK --> AMBIGUITY_CHECK
  AMBIGUITY_CHECK --> AWAITING_CLARIFICATION: definitions differ materially (probe values shown)
  AWAITING_CLARIFICATION --> AMBIGUITY_CHECK: user picks an option
  AMBIGUITY_CHECK --> PLANNING
  PLANNING --> AWAITING_NARROWING: result would be too large
  AWAITING_NARROWING --> PLANNING: user picks a narrower option
  PLANNING --> EXECUTION
  state EXECUTION {
    SQL_GENERATION --> VALIDATION
    VALIDATION --> SQL_GENERATION: rule violation, repair (max 2 attempts)
    VALIDATION --> RUN: RLS injected, cost OK
  }
  EXECUTION --> ANALYSIS: DQ checks + decomposition + drill-downs
  ANALYSIS --> COMPOSITION
  COMPOSITION --> GROUNDING_CHECK
  GROUNDING_CHECK --> COMPOSITION: unmatched number, recompose (max 2)
  GROUNDING_CHECK --> ANSWERED
  GROUNDING_CHECK --> ANSWERED_LOW_CONFIDENCE
  GROUNDING_CHECK --> ANSWERED_WITH_DQ_BLOCK
  ANSWERED --> [*]
```

## Defence in depth for data access

A request reaches the warehouse only after passing every layer. Each layer would hold even if the one before it were bypassed.

| Layer | Where | What it stops |
|---|---|---|
| 1. Intent pre-check | `policies.py` (`Policy.authorize`) | A restricted metric, table or row scope, refused before any SQL exists, with alternatives the role can use |
| 2. Planner uses the semantic layer only | `planner.py`, `semantic.py` | Queries compiled from governed metric definitions, not free-form SQL, where possible |
| 3. SQL validator (sqlglot AST) | `sqlsafety.py` S01–S16 | Statements other than SELECT, several statements, file and extension access, system tables, tables or columns the role cannot use, row-level output for aggregate-only roles, missing time filters, oversized results |
| 4. Row-level security rewrite | `sqlsafety.py` | Every table reference, including inside subqueries and CTEs, is wrapped with the role's row predicate (e.g. `region = 'WEST'`); the SQL that ran is shown in the evidence |
| 5. Cost guard | `EXPLAIN` before execution | Scans above the configured row estimate go to narrowing instead |
| 6. Sealed executor | `executor.py` | DuckDB opened read-only with configuration locked; refuses any query not sealed by the validator; timeout and row cap |
| 7. Audit | `store.py` `access_audit` | Every execution, refusal and rejected request is logged with user, role and predicates |

## Where the numbers come from

```mermaid
flowchart TB
  Q["Executed query results<br/>(with numerator and denominator for ratios)"] --> D["Decomposition<br/>mix effect + rate effect, exact, no residual"]
  Q --> RK["Driver ranking<br/>concentration, volume floor, significance"]
  D --> RK
  RK --> DD["Drill-down<br/>next dimension, hour window, failure codes"]
  Q --> REC["Reconciliation<br/>dashboard vs finance ledger, factor by factor"]
  D & RK & DD & REC --> H["Hypotheses and recommendations<br/>templates over measured patterns, with impact estimates"]
  H --> C["Composer: draft answer, model rewording"]
  C --> G{"Number grounding<br/>every number matches a computed value?"}
  G -- yes --> A["Answer + charts + evidence"]
  G -- no --> C
```

The model may reword the draft, but it cannot introduce a number: the grounding check compares every figure in the headline, facts and recommendations with the values the analysis computed, allowing for display rounding. A rewrite that fails the check is discarded, and the deterministic draft is used instead.

## Data model

Payments star schema in DuckDB ([`analyst/datagen.py`](../analyst/datagen.py)):
- **Facts:** `fact_payment_attempts_raw` (about 1M attempts, with planted duplicates), deduplicated through the view `v_attempts`; orders through the view `v_orders`; `fact_settlements`; and `fact_refunds`.
- **Dimensions:** `dim_merchant` (SCD2: one surrogate key per version, so joins are point-in-time), `dim_payment_method`, `dim_issuer_bank`, `dim_acquirer`, `dim_failure_reason` and `dim_date`.
- **Finance source:** `finance_daily_settlement_summary`, an independent ledger that disagrees with the dashboard on purpose (timezone day boundaries, reversed, failed and held settlements, missing merchants).

`_data_versions` stamps every table. The version goes into every answer, every query record and the cache key.
