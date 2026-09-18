# Causiq — Phase 0 Foundation Plan

**Phase:** P0 — Walking Skeleton
**Status:** Approved — P0.1 through P0.7 complete
**Depends on:** `ENGINEERING_CONTRACT.md` v1.0
**Last updated:** 2026-09-17

---

## 1. The Phase 0 Thesis

Phase 0 is not scaffolding. It is a **complete vertical slice**: one real data-reliability
incident, investigated by one real Claude agent, using one real authorized tool against a real
SQL warehouse, producing a schema-validated root-cause analysis in which every claim resolves to
a recorded piece of evidence, with an append-only audit trail and a green offline test suite.

Every later phase widens this slice; none of them changes its shape.

The point of doing it this way is that the two hardest properties of the eventual system —
*evidence-backed* (I1) and *authorized* (I4) — are load-bearing from the first commit. They are
extremely expensive to add to an agent that already works without them, and nearly free to build
in at the start.

**Review script:** *"Phase 0 answers one question: can this system make a claim it cannot back
up? The answer has to be no before anything else gets built, because every subsequent feature —
sub-agents, remediation, evaluation — inherits that guarantee or destroys it."*

---

## 2. The Seeded Incident (INC-001)

### 2.1 The scenario

A daily revenue aggregate silently under-reports after an upstream change.

```
raw.orders                      source table, one row per order
  order_id, customer_id, order_ts, status, amount_usd

analytics.revenue_daily         daily aggregate, built from raw.orders
  order_date, order_count, total_revenue_usd
```

**The planted defect.** The aggregate is defined as
`... WHERE status = 'COMPLETED' GROUP BY order_date`. On **2026-09-07** the upstream order
service began emitting a new terminal-ish status, `PENDING_CAPTURE`, for card orders awaiting
settlement. Roughly 80% of that day's orders carry the new status. Order volume in `raw.orders`
is unchanged; `revenue_daily.total_revenue_usd` drops ~82%.

This is a *silent* data-quality failure: no pipeline error, no failed task, no exception. The
row count in the source is healthy. Only the semantics changed.

### 2.2 Why this incident was chosen

- **It is genuinely representative.** Unhandled new enum values from an upstream producer are one
  of the most common real causes of silent aggregate drift in data warehouses.
- **It is solvable with the single Phase 0 tool.** The full causal chain is discoverable through
  SQL alone, so the slice needs exactly one tool and no artifact parsers yet.
- **It has one correct answer.** The defect is planted deliberately, so the true root cause is
  known and can be asserted — which is what makes Phase 4 evaluation possible later.
- **It sets up Phase 1 cleanly.** In P0 the agent infers the cause *from the data*. In P1 it will
  **confirm** the same cause from the dbt model SQL, the git commit that introduced the upstream
  status, and the deployment timestamp. Same incident, deeper evidence — a clean demonstration of
  progressive complexity rather than a new toy.

### 2.3 The expected investigation path

| Turn | Agent action | Evidence produced |
|---|---|---|
| 1 | Query `analytics.revenue_daily` for the last 14 days | Confirms the magnitude and exact date of the drop |
| 2 | Query `raw.orders` row counts and revenue by day | Source volume and gross amount are healthy — rules out data loss |
| 3 | Query `raw.orders` grouped by `status` and day | `PENDING_CAPTURE` first appears 2026-09-07 and covers ~80% of rows |
| 4 | Query the excluded rows' summed amount | The excluded amount reconciles exactly with the missing revenue |
| 5 | Emit `Analysis` | Root cause + citations to the four evidence records |

**Expected root cause:** the aggregate filters on `status = 'COMPLETED'`; an upstream change
introduced `PENDING_CAPTURE` on 2026-09-07, and those orders are silently excluded. The drop is
an aggregation-semantics defect, not data loss.

**A note on honesty:** the agent is *not* told this path. If it reaches the answer by another
route, that is fine — the acceptance criterion is a correct, fully-cited conclusion, not a
prescribed sequence. If it cannot reach it, `INCONCLUSIVE` with cited evidence is a valid
outcome under I8, and it becomes a Phase 1 prompt- and tool-design input rather than a hidden
failure.

---

## 3. Scope — What Phase 0 Builds

### P0.1 — Project foundation

**Deliverable:** `pyproject.toml`, committed `uv.lock`, `Makefile`, `.env.example`, `ruff` and
`mypy --strict` configuration, `pytest` configuration with markers, directory skeleton, README.

- **Why it exists:** every later phase's quality gate runs through this. A repo that cannot lint,
  type-check, and test in one command will not do it consistently.
- **How we test it:** `make lint type test` is green on an empty codebase before any feature
  lands — the gate is proven before it has anything to protect.
- **Review script:** *"One command reproduces CI locally. The lockfile is committed, so my
  machine, CI, and the Phase 7 container image resolve identical dependencies."*

### P0.2 — Configuration, logging, identifiers, error taxonomy

**Deliverable:** `config.py` (pydantic-settings, validated at import of the app entrypoint, fails
fast with a readable message), `logging.py` (structlog JSON with bound `run_id` / `incident_id` /
`agent_id`), `ids.py` (prefixed sortable ids: `run_…`, `ev_…`, `inc_…`), `errors.py` (the full
taxonomy from contract §8).

- **What problem it solves:** an agent that fails at minute nine of a run with an unclassified
  exception and unstructured logs is undebuggable. Classification is what makes retry policy and
  recovery decidable rather than guessed.
- **How we test it:** config tests for missing/invalid values; a test asserting every taxonomy
  class is reachable and correctly classified as recoverable or terminal; a log-capture test
  asserting correlation ids appear on every emitted line.

### P0.3 — Domain model and evidence ledger

**Deliverable:** `domain/` (Incident, Evidence, Analysis, InvestigationRun, Budget, terminal
states), `evidence/ledger.py` (append-only ledger, content digest, `validate_citations()`).

- **Why it exists:** invariants I1, I2, I5, I8 live here.
- **The critical function:** `validate_citations(analysis, ledger)` — returns the set of cited
  ids that do not resolve. A non-empty set rejects the analysis. This is the single most
  important twenty lines in the codebase.
- **How we test it:** 100% coverage required. Round-trip serialization; ledger append-only
  enforcement; digest stability; citation validation against empty, partial, fabricated, and
  duplicate citation sets; budget arithmetic at and past every limit.
- **Review script:** *"This is the enforcement point for the whole 'evidence-backed' claim. The
  model can produce whatever citations it likes; only ones the harness actually recorded survive
  validation."*

### P0.4 — Tool layer: protocol, registry, identity, authorization, executor

**Deliverable:** `tools/base.py`, `tools/registry.py` (deterministic name-sorted schema
emission), `tools/authz.py` (`AgentIdentity`, `Permission`, authorization decision),
`tools/executor.py` (authorize → validate → execute under timeout → record evidence → audit).

- **Why it exists:** requirements 19 and 20, and invariants I3 and I4.
- **Design note:** every tool declares `strict: True` with `additionalProperties: False`, so tool
  arguments are schema-valid by construction and the executor's input validation is a
  belt-and-braces second check rather than the primary defence.
- **How we test it:** authorization allow and deny paths; a deliberately-registered test-only
  *mutating* tool that must be denied for lack of an approval token (this proves the Phase 5 gate
  exists in Phase 0); timeout behaviour; that a tool failure produces an `is_error` tool result
  and does **not** end the run; that the registry emits byte-identical schema JSON across calls.
- **Review script:** *"There is exactly one code path from a model intent to a real system call,
  and it checks identity and permission before anything else happens. I can show you the deny
  test — including for a write tool that doesn't exist yet, because the gate is what matters, not
  the tool."*

### P0.5 — Evidence substrate and the `query_warehouse` tool — delivered

**Delivered:** `fixtures/warehouse/seed.sql`, `fixtures/incidents/INC-001.json`,
`src/causiq/evidence_substrate.py` (the fixture builder and incident loader — kept out of
`tools/` since it writes, and the query tool must never share a module with anything that does),
`scripts/seed_warehouse.py` (thin CLI wrapper), `src/causiq/tools/sql_policy.py` (statement-shape
validation), `src/causiq/tools/warehouse.py` (the tool).

Tool contract, as built:

```
name:        query_warehouse
permission:  warehouse.read
mutating:    False
risk:        low
input:       { sql: string, reason: string }     strict, additionalProperties: false
defaults:    max_rows=500 · max_result_bytes=256_000
             query_timeout_seconds=5.0 (DuckDB interrupt() — genuine cancellation)
             outer_timeout_seconds=10.0 (executor's abandon-only backstop; P0.4's
             known limitation, still present, now a last resort rather than the
             primary mechanism)
guards:      three independent layers — see ADR-0007 for the full empirical basis:
               1. statement-shape validation (exactly one statement, DuckDB
                  classifies it StatementType.SELECT, and the caller's own text
                  begins with the literal keyword SELECT or WITH — closing a
                  documented gap where PRAGMA/SHOW/DESCRIBE/SUMMARIZE all
                  classify as SELECT)
               2. a fresh connection per query: read-only + external access
                  disabled + configuration locked, at the DuckDB engine itself
               3. bounded execution: incremental row cap (proven to bound engine
                  work, not just truncate a full result), result-byte cap, and a
                  timer-driven interrupt() timeout
records:     Evidence{ request: sql + reason, content: {sql, columns, rows, row_count,
             truncated}, digest, provenance }
```

- **Why `reason` is a required argument:** it forces the model to state *why* it is asking before
  it asks, which lands in the audit trail and becomes directly gradeable by the Phase 4 tool-use
  evaluator. It costs one schema field and buys an explainability signal.
- **How we test it:** `tests/unit/test_sql_policy.py` (55 tests — every required rejection
  category, parsing edge cases, a named regression test for the PRAGMA gap found during
  implementation), `tests/unit/test_warehouse_tool.py` (limits, a *timed* proof that a slow
  query is genuinely cancelled rather than abandoned, full executor integration, and a
  structural proof that `Tool.run`'s signature has no ledger/journal parameter — direct
  invocation cannot produce evidence), `tests/unit/test_evidence_substrate.py` (fixture
  determinism via digest comparison across two independent builds), and
  `tests/integration/test_inc001_investigation.py` — the incident's root cause derived entirely
  from real query results, never from a hardcoded string.
- **Review script:** *"The tool is narrow on purpose. It is read-only at the connection level,
  not just by convention, and the query text is recorded verbatim — so any claim in the analysis
  can be re-executed by hand against the same database. And the timeout isn't P0.4's
  abandon-and-hope-for-the-best anymore — I can show you the test that proves a query which
  would run for tens of seconds is actually aborted in a fraction of one."*

### P0.6 — Model client port, the bounded agent, and citation enforcement — delivered

**Delivered:** `llm/ports.py` (`ModelClient` protocol and turn/message vocabulary),
`llm/anthropic_client.py`, `llm/fake_client.py`, `llm/prompts/investigator_system.md`,
`agents/investigator.py` (the bounded loop).

This phase widened beyond the plan's original P0.6/P0.7 split: rather than landing the model
client port alone and deferring the agent loop, P0.6 delivered the port **and** a real bounded
investigator that drives it end to end through the existing P0.4 tool boundary, because a
`ModelClient` with nothing to exercise it end to end is unreviewable in isolation. `runner.py` and
`cli.py` — the operator-facing CLI surface — remain P0.7; `audit/journal.py` and `obs/` already
existed from P0.4 and needed no changes.

The Anthropic adapter is the only file in the repo that imports `anthropic`. It owns: client
construction, the model/effort/thinking parameters from contract §6.2, cache breakpoint placement
per §6.3, the typed-exception chain per §8, and usage capture. It calls `messages.create(...)`
directly rather than `messages.parse(...)`, and does not retry a transient failure — both are
deliberate P0.6 decisions with a real-SDK basis, recorded in **ADR-0008**, not silent narrowings
of the contract.

The agent (`Investigator.investigate()`) runs a bounded turn loop against the *existing*
`Budget`/`BudgetTracker` (P0.3) and the *existing* `ToolRegistry` → `ToolExecutor` → `AuthZ` →
`EvidenceLedger` chain (P0.4/P0.5) — no new execution path, no direct DuckDB or ledger access from
the agent. Every tool call the model requests becomes the same `ToolRequest` P0.4 already defined.
The loop ends by calling the *existing* `validate_citations()` (P0.3); a fabricated citation is
rejected there, not by anything new in P0.6.

- **Why the fake adapter is not a shortcut:** the SDK's 1.x transport is `httpx2`-based, so
  transport-level HTTP mocking is awkward and brittle. Testing at the port is both easier and
  more meaningful: `FakeModelClient` replays a deterministic script — including callables that
  read real, ledger-generated evidence ids back out of the conversation history, so no test ever
  hardcodes an id like `ev_001`.
- **How we test it:** offline — the full INC-001 investigation against a real DuckDB file and the
  real P0.4/P0.5 stack with only the model faked; every negative case named in the Engineering
  Contract (fabricated citation, unauthorized tool, tool failure, tool timeout, turn-budget
  exhaustion, tool-call-budget exhaustion, malformed model response). Live (skipped without
  `ANTHROPIC_API_KEY`, never required for `make test`) — two narrow tests asserting the adapter's
  own contract: a structured final analysis, and a requested tool call.
- **Review script:** *"One file talks to Anthropic. Everything else talks to a protocol. That's
  why the suite runs offline, and it's also the seam that makes a Bedrock or Vertex client a
  configuration change in Phase 7 rather than a rewrite. And the agent never touches DuckDB or the
  ledger directly — I can show you that `QueryWarehouseTool.run` doesn't even accept a ledger
  argument, so there's no code path for the agent to fabricate evidence through."*

### P0.7 — The CLI runner, run/audit persistence, and the remaining acceptance gaps — delivered

**Delivered:** `runner.py`, `cli.py`, `docs/architecture/{execution-flow,data-flow}.md`, a
`[project.scripts]` entry point (`causiq`), a `runs_dir` setting alongside the existing
`audit_dir`, and the two `ModelTurn` cache-usage fields described below. `audit/journal.py` and
`obs/ports.py` + `obs/noop.py` already existed (P0.4) and are reused unchanged by
`Investigator.investigate()`.

- `causiq.runner.run_investigation()` is the whole orchestration: pick a `ModelClient`
  (`FakeModelClient` for `--dry-run`, `AnthropicModelClient.from_settings(...)` otherwise), build
  the real `ToolRegistry`/`QueryWarehouseTool` (auto-building the DuckDB file on first use via the
  same `evidence_substrate.build_warehouse_db` the seed script already used), construct the
  existing `Investigator` with `Settings.default_budget()`, run it with a `JsonlAuditSink`, and
  persist the terminal `InvestigationRun` — evidence included — to `{runs_dir}/{run_id}.json`.
  `Investigator.investigate()` itself needed no change at all.
- CLI: `causiq investigate <incident-id> [--dry-run] [--json]`, argparse rather than a third-party
  framework (one subcommand, two flags did not clear the dependency-policy bar for adding one). A
  three-tier exit code (`0` completed, `1` any other terminal state, `2` a usage/configuration
  error before any run started) makes the result scriptable without collapsing `INCONCLUSIVE`,
  `BUDGET_EXCEEDED`, and `FAILED` into an undifferentiated nonzero code's opposite - a "did it
  reach a confirmed root cause" question.
- **AC-9's persistence, precisely:** `InvestigationRun` (P0.3) already carried the run's full
  evidence (`ledger.snapshot()`, unchanged since P0.6), so "persist a partial ledger" needed no new
  domain model - only somewhere to write the record that already existed. See `causiq.runner`'s
  module docstring for the exact two files written per run and the one honestly-stated limitation
  (the single-file result write is not itself crash-atomic; the audit journal, flushed
  incrementally throughout the run, is what actually survives a mid-run crash).
- **Cache usage (AC-16):** `ModelTurn` gained `cache_read_input_tokens`/`cache_creation_input_tokens`
  (additive, default `0`), threaded from the real `Usage.cache_read_input_tokens`/
  `.cache_creation_input_tokens` (both `Optional[int]` on the real SDK type, normalized to `0`).
  A live, opt-in test performs a two-call exchange sharing an identical, deliberately-padded
  system+tools prefix and asserts the second call's `cache_read_input_tokens > 0`.

- **How we test it:** offline - CLI argument parsing, `--dry-run` success in both output modes, an
  unknown incident, a live invocation with no key, a budget-exceeded run's exit code and persisted
  partial evidence, and a dedicated no-network proof for the whole CLI/runner path (not just the
  P0.1-P0.3 pieces the older offline-guarantee test covered). A new integration test
  (`test_real_agent_determinism.py`) proves AC-10 against the *actual* `Investigator`, closing the
  gap the original hand-simulated determinism test left open. Live (skipped without a key): the
  two-call cache-hit assertion above.
- **Review script:** *"`--dry-run` executes the entire pipeline - real agent, real tool executor,
  real DuckDB - against the fake client with no API spend. That's the demo I can run anywhere,
  including offline, and it exercises exactly the same code path as a live run, down to where the
  result gets written."*

---

## 4. Acceptance Criteria

Phase 0 is complete when **every** item below passes. These are checks, not aspirations.

### Functional

- **AC-1** `causiq investigate INC-001` completes against the live API and produces an `Analysis`
  whose `outcome` is `COMPLETED` and whose stated root cause identifies the unhandled
  `PENDING_CAPTURE` status as the reason for the revenue drop. **P0.7 delivered the mechanism**
  (the command runs live once `ANTHROPIC_API_KEY` is set); the live run itself has not yet been
  executed in this environment (no key available) - an operational step, not a remaining code gap.
- **AC-2** The analysis cites at least two distinct evidence records, and **every** cited id
  resolves in that run's ledger. Proven offline (`--dry-run`, 3 evidence records); the live half
  shares AC-1's operational caveat.
- **AC-3** Each evidence record contains the verbatim SQL, the stated reason, the result content,
  a content digest, the acting `agent_id`, and a timestamp.
- **AC-4** The run writes an append-only audit journal from which the full sequence — every model
  call, every authorization decision, every tool execution, the final validation — can be
  reconstructed without reading application logs.
- **AC-5** `causiq investigate INC-001 --dry-run` produces the same terminal state and a
  schema-valid analysis using the fake client, with zero network calls. **Delivered in P0.7** -
  `causiq.cli`/`causiq.runner`, proven by `tests/unit/test_cli.py` and
  `tests/integration/test_offline_guarantee.py`.

### Invariants

- **AC-6** An analysis containing a fabricated `evidence_id` is **rejected**; the run terminates
  in a non-`COMPLETED` state and the rejection is recorded. *(I1)*
- **AC-7** A tool call from an identity lacking the required permission is denied, returns an
  `is_error` tool result, is audited, and does **not** terminate the run. *(I4, §8)*
- **AC-8** A test-only mutating tool is denied for absence of an approval token, even when the
  identity holds the permission. *(I4 — proves the Phase 5 gate)*
- **AC-9** A run that exceeds its turn, token, or deadline budget terminates cleanly in
  `BUDGET_EXCEEDED` with a persisted partial ledger and journal. *(I5, I8)* **Delivered in P0.7** -
  the persisted `InvestigationRun` record already carried the ledger's content (P0.3's `evidence`
  field); P0.7 added the code path that actually writes it to disk. See
  `tests/unit/test_runner.py::test_budget_exceeded_run_still_persists_its_partial_evidence`.
- **AC-10** Two consecutive offline runs with the frozen clock produce byte-identical audit
  journals. *(I6)* **Extended in P0.7** - the pre-existing test proved this only for a
  hand-simulated pre-P0.6 slice; `tests/integration/test_real_agent_determinism.py` now proves it
  against the actual `Investigator`.
- **AC-11** Every non-`SELECT`/`WITH` statement submitted to `query_warehouse` is rejected before
  execution. *(§7.3)*

### Engineering quality

- **AC-12** `make lint type test` is green: `ruff` clean, `mypy --strict` clean on `src/`, and the
  full offline suite passes with **no `ANTHROPIC_API_KEY` set and no network access**.
- **AC-13** Coverage ≥ 85% overall on `src/causiq/`, with 100% on `domain/`, `evidence/`, and
  `authz/`. (Wording corrected in P0.7: `authz` became its own top-level package in P0.3, not
  `tools/authz.py` as first drafted here - see Engineering Contract §4 for why. The actual gate,
  unchanged throughout, also covers `tools/` in full.)
- **AC-14** Live-API tests are visibly **skipped** (not passed, not errored) when no key is
  present, and pass when one is.
- **AC-15** CI runs the offline suite on every push and is green.
- **AC-16** A two-turn live run records `usage.cache_read_input_tokens > 0`, proving the
  prompt-cache prefix is stable. *(§6.3)* **Mechanism delivered in P0.7** - `ModelTurn` carries
  `cache_read_input_tokens`/`cache_creation_input_tokens`, threaded from the real adapter, and
  `tests/integration/test_anthropic_live.py::test_live_second_call_hits_the_prompt_cache` asserts
  exactly this. Not yet executed against the live API in this environment (no key available) -
  the same operational caveat as AC-1.

### Documentation

- **AC-17** `README.md` gets a new engineer from clone to a green `--dry-run` in under five
  minutes, on Windows and on Linux. **Delivered in P0.7** - the quickstart section runs
  `causiq investigate INC-001 --dry-run` directly; manually verified in this environment (Windows).
- **AC-18** `docs/architecture/` contains the execution-flow and data-flow diagrams matching the
  implementation, and ADRs 0001–0005 exist as numbered files. **Delivered in P0.7** -
  `docs/architecture/execution-flow.md` and `data-flow.md`; ADRs now run 0001–0008.
- **AC-19** Every module carries a maturity label. Phase 0 ships **nothing** labelled
  `production-ready` — observability lands in Phase 3, so the §11 checklist cannot yet be
  satisfied. Target label for Phase 0 components is `hardened`.

---

## 5. Explicitly NOT in Phase 0

Listing these is as important as the scope itself. Each is deferred for a stated reason, not
forgotten.

| Not building | Why not yet |
|---|---|
| Multiple agents, an orchestrator, sub-agent decomposition | Orchestration without a working single agent is architecture without evidence. P2. |
| A2A messaging, message bus, agent registry | There is one agent. A protocol between one participant is ceremony. P2. |
| dbt / Airflow / git / deploy / metrics tools | One tool proves the tool *contract*. Five tools before the contract is proven means five rewrites. P1. |
| MCP client or MCP server | Native tool use proves the capability layer first; MCP is a transport for the same contract. P6. |
| OpenTelemetry, Langfuse | The seam and the span vocabulary land in P0; the exporters land in P3, when there is a multi-agent run whose shape is worth tracing. |
| LLM-as-a-Judge, eval harness, golden set | Evaluation needs a stable output contract and more than one incident to be meaningful. P4. |
| Remediation planning, approval workflow, verification | There is no mutating tool. The *gate* is built and tested in P0 (AC-8); the workflow it protects is P5. |
| Streaming, compaction, context editing | Context is a few thousand tokens against a 1M window. These solve problems P0 does not have. P2 introduces them when the ledger grows. |
| Database, queue, web API, auth service | The CLI is a sufficient delivery surface, JSONL is a sufficient sink, and every one of these is a P7 deployment concern. |
| Docker, ECS, Kubernetes, Lambda | Containerizing an unfinished vertical slice means rebuilding the image every phase. P7. |
| Retry/resume of a *partially completed* run | Bounded termination and a reconstructable journal are the P0 requirement; resumability needs durable state. P5. |
| Multiple incidents, incident classes, synthetic incident generation | One incident, solved correctly and reproducibly, is worth more than five solved ambiguously. P1. |

**Review script:** *"The deferral list is a design artifact, not an apology. Each line names the
phase that will build it and the precondition it's waiting on. That's how the architecture stays
explainable — nothing exists in this repo that I can't tell you the reason for."*

---

## 6. Test Plan Summary

| Suite | Count (target) | Runtime | Key assertions |
|---|---|---|---|
| `tests/unit/` | ~45 | < 2s | Domain round-trips, budget arithmetic, citation validation, authorization decisions, error classification, context assembly ordering |
| `tests/contract/` | ~12 | < 1s | Analysis JSON Schema stability, tool schema byte-stability, system-prompt checksum, taxonomy completeness |
| `tests/integration/` | ~15 | < 20s | Real DuckDB against the seed; full loop with the fake client; every `stop_reason`; SQL guard rejections; journal determinism |
| `tests/integration/live/` | 3 | ~60s | One real structured-output call; the two-turn cache-hit assertion; one real end-to-end investigation |

The system-prompt checksum test deserves a note: it fails whenever the prompt file changes. That
is intentional — a prompt change is a behaviour change, and forcing it to be acknowledged in a
commit is what makes prompt regressions traceable in Phase 4.

---

## 7. Cost and Risk

**Estimated API spend.** A full live run is roughly 5 model calls; with cache hits, on the order
of 50–80K cumulative input tokens and 5–8K output tokens, which at Opus 5 rates ($5 / $25 per
MTok) is approximately **$0.40–$0.60 per end-to-end run**. The offline suite — which is what CI
runs and what developers run all day — costs nothing. Treat the figures as an estimate to be
replaced by the measured `usage` totals the run record will carry.

**Risks and mitigations.**

| Risk | Mitigation |
|---|---|
| The agent reaches the right answer by guessing rather than by evidence | AC-2 requires resolvable citations; AC-6 rejects fabricated ones. A lucky guess without evidence fails. |
| One incident overfits the prompt | Acknowledged and accepted for P0. P1 adds distinct incident classes precisely to detect it. |
| Structured-output schema too rigid, forcing bad analyses into it | `INCONCLUSIVE` plus a free-text `limitations` field is part of the schema (I8), so the model has an honest exit. |
| Cache instability silently doubles cost | AC-16 asserts a cache hit; the run record stores cache token counts. |
| Scope creep into P1 during implementation | The §5 deferral list is the acceptance boundary. Anything on it that appears in the diff gets removed in review. |

---

## 8. Phase 0 Review Demonstration

The order to present it in, and what each step proves:

1. `make test` with no API key and no network — proves I6 and AC-12.
2. `causiq investigate INC-001 --dry-run` — the whole pipeline, free and deterministic.
3. `causiq investigate INC-001` live — the real agent, the real finding.
4. Open the audit journal and walk one tool call: intent → authorization → execution → evidence →
   digest — proves I2, I3, I7.
5. Take one sentence from the analysis, follow its citation to the evidence record, and
   re-execute that SQL by hand against the DuckDB file — proves I1 end to end, and this is the
   moment the project stops being a demo.
6. Run the fabricated-citation test and the permission-denial test — proves the guarantees hold
   when something goes wrong, which is the only time guarantees matter.

---

## 9. Definition of Done for Phase 0

- [ ] All acceptance criteria AC-1 … AC-19 pass
- [ ] ADRs 0001–0005 committed as numbered files
- [ ] Architecture diagrams match the implementation
- [ ] README verified from a clean clone
- [ ] Every module carries a maturity label; none claims `production-ready`
- [ ] The §5 deferral list holds — no P1+ scope in the diff
- [ ] Reviewed against contract §2 invariants
- [ ] Committed with a conventional-commit history and a green CI run

Only then does Phase 1 planning begin.
