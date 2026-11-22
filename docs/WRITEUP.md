# Enterprise AI Business Analyst: design write-up

## What was asked

Build an assistant that answers business questions about payments data in plain English. It should:
- query at least two related datasets;
- show its work (queries, steps, evidence);
- separate facts from hypotheses and recommendations;
- add charts;
- behave correctly when things go wrong: duplicated or missing records, a user asking beyond their access, two sources that disagree, a metric change caused by mix rather than performance, an ambiguous period or denominator, and a query that is expensive or unsafe.

## What was built

A working system ([architecture](ARCHITECTURE.md)):
- **Warehouse:** a synthetic payments warehouse of about 1M attempts in DuckDB. It has a star schema, a settlements fact and an independent finance ledger. Ten anomalies were planted with known answers ([ground truth](../fixtures/ground_truth.json)).
- **Semantic layer:** metrics, dimensions and synonyms, with several definitions of "success rate".
- **Policies:** six roles, with row-level scopes.
- **Question pipeline:** parse → access pre-check → ambiguity check → plan → generate SQL → validate → execute → data-quality checks → analyse → compose → ground-check, streamed live.
- **SQL safety gate:** sqlglot AST rules, a row-level security rewrite, an EXPLAIN cost guard, and a sealed read-only executor.
- **Analysis engine:** exact mix/rate decomposition, driver ranking, drill-down, hour-window and failure-code detection, and source reconciliation.
- **Composer:** facts / hypotheses / recommendations, a confidence score, chart specs, and a check that every number appears in the computed results.
- **API:** FastAPI with server-sent events. A SQLite metadata DB holds sessions, every run and query, the access audit, a result cache, feedback and eval runs.
- **Web UI:** a no-build page with a role switcher, live progress, clarification forms, the answer card, charts, evidence (SQL, access rules applied, rows, re-run) and a steps timeline.
- **Eval harness:** 20 known-answer cases ([report](../evals/REPORT.md)), with a terminal demo and a timed [demo script](DEMO.md).

Tests: 313 unit and integration tests, plus full-warehouse checks that re-derive the ground truth through the semantic layer, the safety gate and the pipeline.

## The central design decision

**The language model proposes; deterministic code decides.** An analyst that invents a number is worse than no analyst, so the model never has the last word on anything checkable:

| Concern | Who decides | Why |
|---|---|---|
| What the user means | Model (or offline rules), then validated against the semantic layer | Language is the model's strength; unknown metrics or values are rejected, not guessed |
| Which definition | The user, when definitions differ materially | The system probes each definition and shows its value; it asks only when the difference matters (≥ 5%) |
| What may run | Policies + AST validator + RLS rewrite + cost guard | Never trust generated SQL; enforcement is independent of the generator |
| Every number | Executed queries + deterministic analysis code | Decomposition, ranking and reconciliation are exact arithmetic, testable against ground truth |
| The wording | Model, under a grounding check | A rewrite that introduces any number not in the computed results is discarded |

So the system degrades gracefully. With no API key it runs fully offline, with rule-based parsing and template wording, and still passes every eval case. With Claude or Gemini, the model improves the language, not the numbers.

## How each failure scenario is handled

**Missing or duplicated records.** Ten data-quality checks run on every analysis window.
- **Duplicates (Oct 5, 3% replayed records):** every metric reads the deduplicated view `v_attempts`, and the answer says so: "the numbers shown are not affected".
- **Missing merchant keys (up to 8.5% on Oct 18–20):** breakdowns show an explicit *Unknown* bucket with a caveat ("segment totals undercount by that share") and confidence drops to MEDIUM. Above 10% the merchant breakdown would be blocked while overall metrics still answer.
- **Late-arriving data (Oct 28 at 80% volume):** that day is excluded. "Did success rate drop yesterday?" gets a premise correction, not a false alarm.

**A user asks beyond their access.** Enforcement has several independent layers:
- the pre-check refuses restricted metrics and tables before SQL exists, and offers alternatives the role can use;
- the validator rejects anything the role can't touch;
- the RLS rewrite injects the row scope into every table reference, including subqueries and CTEs;
- the executor refuses unsealed SQL.

Out-of-scope entities get the same answer as entities that don't exist, so existence doesn't leak. The answer states the scope, the evidence shows the injected predicate, and every refusal is audited.

**Two sources disagree** (dashboard GMV vs the finance ledger, 3.5% apart). The system never silently picks one. It computes both, then attributes the gap factor by factor, to the paisa:
- reversed, failed and held settlements;
- merchants missing from the ledger;
- the IST vs UTC day boundary.

The residual is ₹0, and the answer recommends which source to use for which purpose.

**Success rate falls because of mix, not performance.** Every ratio-metric change is decomposed exactly into a mix effect and a rate effect. The centred formula leaves no residual. Over the last two weeks the drop is almost entirely mix (−2.54pp of −2.63pp): UPI collect (a low-success flow) grew from 13% to 33% of traffic, almost all from one merchant, while its own rate held flat. The composer is forbidden to say a segment "got worse" unless its rate effect is significant, and the eval checks the answer never claims it.

**Ambiguous period or denominator.** Vague periods ("lately") get a clarification question. Precise ones ("last two weeks") get a disclosed reading ("rolling 14 days, not calendar weeks"). For "success rate", the four definitions are probed and shown with their values (85.5%, 82.8%, 93.3%, 92.2%), so the user sees why the choice matters. The choice is remembered for the conversation. The behaviour is configurable: always ask, ask when material, or assume and disclose.

**Expensive or unsafe queries.**
- **Unsafe:** requests to change data, and prompt injections, are rejected before planning. Any SQL that slipped through would still hit the validator (single read-only SELECT only, no file, extension or system access) and a read-only connection. The eval harness records what actually reached the executor and checks no DROP, DELETE, ATTACH or COPY ever did.
- **Expensive:** "hourly success rate for every merchant for a year" is caught by the result-size estimate. It returns concrete narrowing options instead of a 1.1-million-row query.

## Evaluation

[`evals/cases.yaml`](../evals/cases.yaml) has EV01–EV15 from the spec plus five robustness cases. Each runs through the real pipeline on the full warehouse and is graded by up to four graders:
- **numeric:** headline rates, decomposition totals, counts, rows and reconciliation factors against the ground truth;
- **structural:** status, ambiguity types, caveat codes, top driver, scope disclosure, RLS in every executed query, and nothing forbidden at the executor;
- **SQL equivalence:** hand-written reference SQL must return the same result set as the step the pipeline ran;
- **LLM judge:** faithfulness, fact/hypothesis separation and clarity, scored with the evidence in view (query rows and the computed analysis).

Results on the full warehouse:

| Backend | Cases | EV01–EV15 | LLM judge |
|---|---|---|---|
| offline rules ([report](../evals/REPORT.md)) | 20/20 | 15/15 | skipped |
| Gemini, first run ([report](../evals/gemini/run1-REPORT.md)) | 13/20 | 9/15 | 0/3 |
| Gemini, after fixes ([report](../evals/gemini/REPORT.md)) | 19/20 | 14/15 | 2/2 (3 skipped: daily quota) |

**What the real model exposed.** The first Gemini run failed every analysis case, though safety, access and ambiguity held. Gemini (Flash-Lite for parsing) did things a user never asked for:
- picked a success-rate definition itself, which skipped the clarification;
- added unasked breakdowns and a daily grain;
- read "what happened…" and "…and why?" as trends and rankings;
- returned a reconciliation with no metrics and a free-form custom query, which replaced the analysis template;
- for a WEST-scoped user, let "NORTH region" fall away, because the value isn't in their catalogue.

The last one was a bug in my code. The answer was GMV for WEST, which is no leak, since row-level security still applied, but it answered a different question than the one asked.

The fix was not prompt tuning but more of the central principle: the parse must agree with the question's own words. `normalize` now checks each choice against the question text and corrects it deterministically, noting the change in the trace. Each fix has a regression test built from Gemini's actual output.

The run also caught a harness flaw: the judge saw only raw query rows, not the computed analysis the facts come from, so it marked correct numbers as unsupported.

The one remaining Gemini failure moves between runs. The parse is not deterministic, and the guards don't yet cover every reading. Eval results now record the parse, so the next failure can be traced.

The graders are tested to fail wrong answers, not only to pass right ones: a changed expected value, the wrong status, a reference query over the raw (duplicated) table, a simulated DELETE leaking past the gate, and a low judge score all flip the grade.

What the number does not show:
- Gemini ran on the free tier: latency includes rate-limit waits, the judge was cut short by daily quotas, and cost isn't priced. Claude has not been evaluated: the Anthropic key available had no API credits.
- The cases came from the same planted anomalies the analysis was tuned on. They test that the system finds known answers end to end, not how it generalises to new questions.
- The next step would be a held-out set of questions written by someone else, run through Claude, with judge scores.

## Trade-offs and limitations

- **Templates over an LLM planner.** Plans for the spec's question types are deterministic templates over the metric layer; the model writes SQL only for custom requests. This gives reliable, testable analyses at the cost of flexibility for unusual questions.
- **Hypotheses are templates over measured patterns** (mix shift, issuer outage, acquirer degradation, premise not supported, seasonal event). They are labelled hypotheses, with a confidence and a way to check. The data shows the pattern, not the cause.
- **Performance.** Every query on `v_attempts` scans the full raw table, because the view's deduplication window blocks filter pushdown. A driver analysis is 15–20 queries, so 5–20 s. At scale the deduplicated table would be materialised; repeats are already served from the result cache.
- **Mocked parts:**
  - sign-in (an `X-User-Id` header, picked in the UI);
  - the warehouse (DuckDB instead of Snowflake/BigQuery);
  - the data catalogue (YAML);
  - the finance system (a ledger table);
  - the data (synthetic and seeded).

  Event streams are held in process memory, so the API runs as one process.
- **Not built:**
  - a demo video (the [demo script](DEMO.md) and `scripts/demo.py` replace it);
  - SSO;
  - a materialised deduplicated table;
  - multi-process event fan-out;
  - judge scores from a real model run.

## What I would do next

1. Run the eval suite through Claude (`--llm anthropic`) and a paid Gemini tier, several times each, to measure run-to-run variance and get judge scores on every case. Then extend the parse guards wherever failures recur.
2. Write a held-out question set with a payments analyst, phrased differently from the planted cases.
3. Materialise the deduplicated attempts table (and partition by date) so driver analyses take seconds.
4. Replace the mock header with SSO/JWT and map identity-provider groups to roles. Move the event streams onto a shared bus so the API can scale out.
5. Add feedback-driven monitoring: answers rated unhelpful, and grounding retries, become new eval cases.
