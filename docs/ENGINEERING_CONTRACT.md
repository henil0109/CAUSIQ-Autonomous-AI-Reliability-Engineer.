# Causiq — Engineering Contract

**Project:** Causiq — Autonomous AI Reliability Engineer
**Status:** v1.0 approved — Phase 0 in progress (P0.1–P0.6 complete)
**Owner:** Henil Patel
**Last updated:** 2026-09-17

This document is the binding technical agreement for how Causiq is built. It defines the
problem, the invariants, the architecture, the technology decisions and their rationale, and
the standards a component must meet before anyone is allowed to call it production-ready.

Every decision below is written so it can be defended out loud in a technical review. Where a
decision is significant, it carries explicit fields: **Why it exists**, **What problem it
solves**, **Why this approach**, **How it integrates**, **How we test it**, and **Review
script** (how to explain it when challenged).

---

## 1. Problem Statement

Data and software engineering teams lose hours per reliability incident to *evidence
correlation*, not to fixing. The facts needed to explain a failure already exist — in Airflow
run history, dbt artifacts, data-quality results, warehouse tables, schema history, git
commits, deployment logs, and metrics — but they live in separate systems with separate query
languages and no shared timeline. A human engineer manually joins them under time pressure,
inconsistently, and the reasoning is rarely written down in a form the next responder can
audit.

Causiq automates the *investigation*, not the fixing. It retrieves evidence from authorized
systems, correlates it on a shared incident timeline, produces a root-cause analysis in which
every claim is traceable to a specific retrieved fact, proposes remediation with an explicit
risk and reversibility assessment, requires human approval before any state-changing action,
and verifies the outcome afterwards.

### 1.1 What Causiq is

A **bounded autonomous investigator**. It has read authority by default, write authority only
by explicit grant plus human approval, a finite turn and token budget per investigation, and
an append-only audit trail of everything it saw and did.

### 1.2 What Causiq is not

- Not a chatbot. There is no free-form conversational surface in the product path.
- Not a monitoring or alerting system. Causiq consumes incidents; it does not detect them.
- Not an auto-remediation system. Remediation is *proposed*; execution is gated.
- Not a general-purpose agent. Its tool surface is deliberately narrow and typed.

---

## 2. Core Invariants

These are the rules that make Causiq defensible rather than impressive. A change that breaks
one of these is a contract change and requires an ADR, not a pull request comment.

| # | Invariant | Enforcement |
|---|---|---|
| **I1** | **No claim without evidence.** Every assertion in an RCA cites one or more `evidence_id` values that exist in that run's evidence ledger. | Machine-checked post-validation. An analysis citing an unknown or absent id is rejected, not surfaced. |
| **I2** | **Evidence is immutable and attributable.** Every evidence record stores what was asked, what came back, which tool answered, under which agent identity, and when. | Ledger is append-only; records carry a content digest. |
| **I3** | **The model never touches a system directly.** Claude emits tool *intents*; the harness authorizes, executes, records, and returns results. | All I/O lives behind the tool registry; no network or DB client is reachable from prompt-construction code. |
| **I4** | **Read is default, write is granted.** A tool is read-only unless it declares `mutating = True`, and a mutating tool cannot execute without both an identity grant and a human approval token. | Two independent gates in the tool executor. |
| **I5** | **Every run is bounded.** Maximum turns, maximum tool calls, maximum tokens, and wall-clock deadline are configured, enforced, and recorded. | Budget object checked before each model call and each tool call. |
| **I6** | **Every run is reproducible offline.** The full evidence substrate is versioned in the repo; tests never require a live warehouse or a live API key. | Fixture-backed substrate + injected model client port. |
| **I7** | **Every run is auditable.** A third party can reconstruct what happened from persisted records alone, without reading logs or re-running the agent. | Append-only audit journal per run, written before and after each side effect. |
| **I8** | **Failure is a first-class outcome.** "I could not determine the root cause with the available evidence" is a valid, expected, well-structured result. | `AnalysisOutcome.INCONCLUSIVE` is part of the output schema, not an error path. |

**Review script for invariants:** *"The hard problem with an investigation agent isn't getting
it to talk about a failure — it's stopping it from asserting things it didn't verify. I1 and
I2 are the answer: the model can only cite from a ledger the harness populated, and a citation
that doesn't resolve fails validation before the user ever sees it. Everything else in the
architecture exists to keep those two invariants true."*

---

## 3. Domain Model

The domain model is the spine of the system. It is pure data — Pydantic models with no I/O —
so it can be validated, serialized, diffed, and tested without any infrastructure.

```
Incident                 the trigger: what broke, where, when, detected by what
  └─ InvestigationRun        one bounded execution against one incident
       ├─ Budget                 turns / tool calls / tokens / deadline
       ├─ EvidenceLedger         append-only list of Evidence
       │    └─ Evidence          {id, source, tool_name, request, content, digest, collected_at}
       ├─ Hypothesis[]           candidate cause + supporting[] + refuting[]
       ├─ Analysis               outcome, root_cause, confidence, citations[] → evidence ids
       ├─ RemediationPlan        steps[], risk, reversibility, blast_radius     [Phase 5]
       ├─ Approval               decision, approver, scope, expires_at          [Phase 5]
       ├─ Verification           post-remediation re-check                      [Phase 5]
       └─ AuditJournal           append-only record of every decision + side effect
```

- **Why it exists:** because the alternative — passing dicts between agents — makes I1 and I2
  unenforceable and makes evaluation impossible.
- **What problem it solves:** it gives the system a single, typed definition of "what an
  investigation is" that the CLI, the agents, the tests, the evaluators, and the audit trail
  all agree on.
- **Why this approach:** Pydantic v2 gives runtime validation at the boundary, JSON Schema
  generation for free (which we feed directly to Claude's structured-output and strict-tool
  surfaces), and zero-cost typing internally.
- **How it integrates:** these models are the wire format for A2A messages (P2), the payload of
  audit records (all phases), the input to LLM-as-a-Judge evaluation (P4), and the API response
  shape when Causiq is deployed as a service (P7).
- **How we test it:** round-trip tests (serialize → deserialize → equal), schema-stability tests
  that fail loudly when a field changes, and validator tests for I1 (citation resolution) and I5
  (budget arithmetic).
- **Review script:** *"The domain model is deliberately the first thing built, because it is the
  contract between the agent layer and everything that audits the agent layer. The evidence
  ledger and the citation validator are what turn 'the LLM said so' into 'here is the query,
  here is the row it returned, here is the digest'."*

---

## 4. Architecture

### 4.1 Layers

Strict dependency direction — arrows point downward only. Nothing in an inner layer imports
from an outer one.

```
┌──────────────────────────────────────────────────────────────┐
│  Delivery      CLI (P0) → HTTP API / worker (P7)             │
├──────────────────────────────────────────────────────────────┤
│  Orchestration Investigator (P0) → Orchestrator + sub-agents │
│                (P2) → Remediation + approval + verify (P5)   │
├──────────────────────────────────────────────────────────────┤
│  Agent runtime Turn loop · context assembly · budget guard   │
│                · structured-output validation                │
├──────────────────────────────────────────────────────────────┤
│  Capability    Tool registry · authorization · execution     │
│                · evidence recording                          │
├──────────────────────────────────────────────────────────────┤
│  Ports         ModelClient · Tracer · AuditSink · Clock      │
├──────────────────────────────────────────────────────────────┤
│  Domain        Incident · Evidence · Hypothesis · Analysis   │
└──────────────────────────────────────────────────────────────┘
         ↑ adapters plug in at the Ports layer:
         Anthropic SDK · DuckDB · filesystem artifacts · OTel · Langfuse
```

**Why the Ports layer exists:** it is the single reason the system is testable and the single
reason Phase 3 (OpenTelemetry + Langfuse) is a Phase 3 job and not a rewrite. Four small
protocols defined in Phase 0 with trivial implementations cost roughly fifty lines and remove
the entire class of "we can't test this without an API key / we can't add tracing without
touching every call site" problems.

**Review script:** *"There are exactly four seams: how we talk to the model, how we emit
telemetry, how we persist audit records, and how we read the clock. Every one of them is a
protocol with a real adapter and a fake adapter. That's why the test suite runs offline and
deterministically, and why adding Langfuse later is an adapter, not a refactor."*

### 4.2 Execution flow (the Phase 0 slice, and the shape all later phases keep)

```
  incident id
      │
      ▼
  load Incident from fixture store ──────────────► validate against schema
      │
      ▼
  open InvestigationRun (run_id, identity, budget, ledger, audit journal)
      │
      ▼
  ┌── agent turn loop ─────────────────────────────────────────────┐
  │   assemble context = frozen system prompt        [cached]      │
  │                    + tool schemas, name-sorted   [cached]      │
  │                    + incident brief              [volatile]    │
  │                    + prior turns                 [volatile]    │
  │   budget guard: turns / tokens / deadline  ──► exceeded? stop  │
  │   ModelClient.create(...)                                      │
  │   inspect stop_reason:                                         │
  │     end_turn   ─► exit loop                                    │
  │     tool_use   ─► for each tool_use block:                     │
  │                     authorize(identity, tool) ─► deny? error   │
  │                                                  result block  │
  │                     validate input against schema              │
  │                     execute under timeout                      │
  │                     record Evidence (digest + provenance)      │
  │                     append audit entry                         │
  │                   return ALL tool_result blocks in ONE message │
  │     max_tokens ─► recoverable; recorded; bounded retry         │
  │     pause_turn ─► re-send with paused turn appended (capped)   │
  │     refusal    ─► terminal; stop_details recorded              │
  └────────────────────────────────────────────────────────────────┘
      │
      ▼
  structured-output call → Analysis (JSON Schema constrained)
      │
      ▼
  citation validator (I1): every cited id ∈ ledger ──► fail = reject
      │
      ▼
  persist run record + audit journal  ──►  render report to operator
```

### 4.3 Repository architecture

```
causiq/
├── pyproject.toml                 # single source of deps, tool config, package metadata
├── uv.lock                        # pinned, committed — reproducible builds
├── Makefile                       # one verb per workflow: setup/lint/type/test/cov
├── tasks.ps1                      # the same verbs on Windows, where make is absent
├── .github/workflows/ci.yml       # the gate: lint + types + both coverage gates
├── .env.example                   # documented config surface; never a real secret
├── README.md
├── docs/
│   ├── ENGINEERING_CONTRACT.md    # this file
│   ├── PHASE_0_PLAN.md
│   ├── adr/                       # one numbered file per significant decision
│   └── architecture/              # diagrams, execution/data flow, threat model
├── src/causiq/
│   ├── config.py                  # pydantic-settings; validated at process start
│   ├── errors.py                  # error taxonomy (§8)
│   ├── logging.py                 # structlog; run_id/incident_id bound to every line
│   ├── ids.py                     # prefixed, sortable identifiers
│   ├── domain/                    # pure models, zero I/O
│   │   ├── enums.py               # closed vocabularies
│   │   ├── incident.py
│   │   ├── evidence.py
│   │   ├── hypothesis.py
│   │   ├── analysis.py
│   │   ├── budget.py              # Budget + BudgetTracker (invariant I5)
│   │   ├── audit.py               # AuditEntry, AuditEventType
│   │   └── run.py
│   ├── evidence/
│   │   └── ledger.py              # append-only ledger + citation validator (I1)
│   ├── clock.py                   # Clock port: SystemClock + FrozenClock
│   ├── authz/                     # identity and authorization (invariant I4)
│   │   ├── models.py              # AgentIdentity, Permission, ApprovalToken
│   │   └── decision.py            # authorize() - the single gate
│   ├── evidence_substrate.py      # P0.5: build the DuckDB fixture; load incident fixtures
│   ├── tools/
│   │   ├── base.py                # Tool protocol: name, description, schema, permission
│   │   ├── registry.py            # deterministic ordering (prompt-cache stability)
│   │   ├── executor.py            # authorize → validate → execute → record → audit
│   │   ├── sql_policy.py          # P0.5: statement-shape validation (ADR-0007)
│   │   └── warehouse.py           # P0.5: query_warehouse (read-only DuckDB)
│   ├── llm/
│   │   ├── ports.py               # P0.6: ModelClient protocol + turn/message vocabulary
│   │   ├── anthropic_client.py    # P0.6: the real adapter - the only file importing `anthropic`
│   │   ├── fake_client.py         # P0.6: scripted adapter for offline tests
│   │   └── prompts/               # P0.6: versioned prompt text, loaded from files
│   ├── agents/
│   │   └── investigator.py        # P0.6: the bounded loop (turns → tool calls → citation check)
│   ├── audit/
│   │   └── journal.py             # append-only sink (JSONL → durable store in P7)
│   ├── obs/
│   │   ├── ports.py               # Tracer protocol + span-name constants
│   │   └── noop.py                # P0 implementation
│   ├── runner.py                  # P0.7 (not yet built): orchestrates one InvestigationRun via the CLI
│   └── cli.py                     # P0.7 (not yet built): operator surface
├── fixtures/                      # the versioned world Causiq investigates
│   ├── warehouse/seed.sql         # DuckDB schema + seeded rows, including the defect
│   ├── incidents/INC-001.json
│   └── artifacts/                 # P1: dbt, Airflow, git, deploy, DQ results
├── tests/
│   ├── unit/                      # pure logic, no I/O, milliseconds
│   ├── contract/                  # schemas, invariants, prompt/tool-surface stability
│   ├── integration/               # real DuckDB; live API only when a key is present
│   ├── evals/                     # P4: scored agent behaviour
│   └── conftest.py
└── scripts/
    └── seed_warehouse.py
```

- **Why `authz/` is its own package rather than `tools/authz.py`:** authorization is a property
  of identities and capabilities, not of tools. The P0.4 tool executor is one of its *callers*.
  Keeping the dependency pointing that way let the authorization rules - including the Phase 5
  approval gate - be implemented and tested in P0.3, before any tool existed.
- **Why `src/` layout:** it makes it impossible to accidentally test the working directory
  instead of the installed package — a real and common class of false-green test suites.
- **Why fixtures are versioned in-repo:** invariant I6. If the substrate drifts, evaluation
  scores are meaningless and regressions are invisible.

---

## 5. Technology Decisions

### ADR-0001 — Own the agent loop on the Messages API

**Decision:** build Causiq's agent loop directly on the Anthropic Python SDK's
`client.messages.create(...)`, not on the Claude Agent SDK and not on Managed Agents.
**Status:** accepted.

- **Why it exists:** something must decide when to call the model, what context to send, which
  tool calls to permit, and when to stop.
- **What problem it solves:** requirements 19–22 (agent identity, tool permission controls,
  auditability, failure handling) and 23–26 (Docker / serverless / ECS / Kubernetes) are only
  implementable if the loop runs in our process, on our infrastructure, under our authorization
  code. A hosted loop removes the deployment requirements from the project entirely; a
  batteries-included harness makes the authorization gate someone else's code path.
- **Why this approach over the alternatives:** the Claude Agent SDK ships a filesystem/coding-
  shaped tool surface (Read/Write/Edit/Bash/Grep) that is the wrong shape for data-reliability
  evidence, and its harness would be the thing we explain rather than the thing we author.
  Managed Agents is genuinely the simplest option for a hosted stateful agent — and the right
  answer for a different project. We also deliberately skip the SDK's beta `tool_runner`
  helper: the loop is roughly sixty lines, and writing it removes a beta dependency while
  giving us the exact interception point where authorization, evidence recording, and audit
  writes must happen.
- **How it integrates:** the loop sits behind the `ModelClient` port, so the transport can later
  move to `AnthropicBedrockMantle` (AWS) or a Vertex/Foundry client without touching
  orchestration code — relevant to the P7 AWS deployment work.
- **How we test it:** the loop is driven by scripted `FakeModelClient` responses covering every
  `stop_reason` (`end_turn`, `tool_use`, `max_tokens`, `pause_turn`, `refusal`), multi-block
  parallel tool use, and budget exhaustion — all offline and deterministic.
- **Review script:** *"We own the loop because the loop is where the security model lives. Every
  tool call passes through one function that checks identity and permission, executes, records
  evidence with provenance, and writes an audit entry. If a framework owned that loop, I could
  not show you that function."*

### ADR-0002 — No agent framework (no LangChain / LlamaIndex / CrewAI / AutoGen)

**Decision:** no third-party agent orchestration framework at any phase.
**Status:** accepted.

- **What problem it solves:** the project must be explainable end to end. Frameworks obscure the
  exact request sent to the model — which is precisely the artifact under review — add version
  churn on a fast-moving API surface, and impose abstractions (chains, crews, memories) that do
  not map onto our invariants.
- **Why this approach:** the orchestration we actually need — a turn loop, a typed message
  envelope between agents, a budget guard — is a few hundred lines we can defend line by line.
- **Trade-off accepted:** more code, no free integrations. That is the correct trade for a
  system whose value proposition is auditability.
- **Review script:** *"I can show you the exact bytes we send to Claude and explain why each one
  is there. That's a deliberate choice, and it's why the prompt-cache strategy and the context
  budget are measurable rather than emergent."*

### ADR-0003 — Local DuckDB + versioned pipeline artifacts as the evidence substrate

**Decision:** the investigated world is a seeded DuckDB warehouse plus dbt / Airflow / git /
deploy artifacts committed to the repository.
**Status:** accepted.

- **What problem it solves:** agent evaluation (requirements 16–18) requires a known-correct
  answer and a repeatable environment. A live warehouse gives neither, costs money per query,
  and cannot run in CI.
- **Why this approach:** DuckDB is a real SQL engine — the data-quality checks execute genuine
  SQL against genuine tables, so nothing about the investigation is simulated except the
  provenance of the data. The artifacts are real file formats (`dbt run_results.json`, Airflow
  DAG-run payloads), so the parsers we write are the parsers a production deployment needs.
- **How it integrates:** each evidence source sits behind a tool. Swapping DuckDB for Snowflake
  in production is a new adapter behind the same `query_warehouse` tool contract; the agent
  layer does not change.
- **How we test it:** the seed is deterministic and the defect is planted deliberately, so the
  expected root cause is known and assertable.
- **Review script:** *"The substrate is local so the evaluation is honest. I know the true root
  cause of every seeded incident, which means I can score the agent instead of admiring it."*

### ADR-0004 — Ports for model, tracing, audit, and clock defined in Phase 0

**Decision:** four protocols land in Phase 0 with real and fake implementations, even though
tracing is a Phase 3 deliverable.
**Status:** accepted.

- **What problem it solves:** retrofitting observability and testability into an agent loop is
  expensive and usually done badly; defining the seam up front is nearly free.
- **Why this is not speculative generality:** each port has a concrete, immediate consumer in
  Phase 0. `ModelClient` is what makes the loop testable offline; `Clock` is what makes
  timestamps assertable; `AuditSink` is invariant I7; `Tracer` has a no-op implementation and a
  fixed set of span-name constants so Phase 3 is configuration, not surgery.
- **Review script:** *"I added four interfaces, not a plugin system. Each has exactly two
  implementations — the real one and the test one — and each earns its place in Phase 0."*

### ADR-0005 — Structured outputs and strict tools for every machine-consumed model output

**Decision:** any model output another component consumes is produced under a JSON Schema
constraint; free text is only ever for human display.
**Status:** accepted.

- **What problem it solves:** parsing prose is the largest source of silent failure in LLM
  systems, and requirement 9 (evidence-backed RCA) is unenforceable if the RCA is prose.
- **Why this approach:** the API constrains generation to the schema, so failures become rare
  and structural rather than frequent and semantic. Combined with the citation validator, this
  turns "evidence-backed" from a prompt instruction into a checked property.
- **Review script:** *"The prompt asks for citations. The schema requires them. The validator
  proves them. Three layers, because a prompt instruction alone is a request, not a guarantee."*

### 5.1 Dependency set and justification

| Dependency | Why it is here | Why not the alternative |
|---|---|---|
| `anthropic` (official SDK, 1.x) | Mandated by ADR-0001. Typed responses, automatic retry/backoff, typed exception hierarchy, usage accounting. | Raw HTTP loses the typed exception chain the error taxonomy depends on. Note 1.x is built on `httpx2`, not `httpx` — one more reason we test through the port rather than at the transport. |
| `pydantic` v2 + `pydantic-settings` | Domain validation, JSON Schema generation feeding Claude's structured outputs, fail-fast config validation at startup. | Dataclasses give no runtime validation and no schema generation. |
| `duckdb` | ADR-0003. Embedded, zero-ops, real SQL. | Postgres needs a container in every test run. |
| `structlog` | Machine-parseable logs with `run_id`/`incident_id` bound to every line — the precondition for correlating logs with traces in P3. | stdlib `logging` alone produces prose that cannot be correlated. |
| `typer` | The operator surface; P0's delivery mechanism and later the entrypoint the container image invokes. | argparse works but costs more code for typed options and help output. |
| `pytest` (+ `pytest-cov`) | Test runner and coverage gate. | — |
| `ruff` | Lint and format in one fast tool. | black + flake8 + isort is three tools and three configs. |
| `mypy` (strict on `src/`) | The domain model's guarantees are worthless if the code around it is untyped. | — |
| `uv` | Fast, lockfile-based, reproducible environments; committed `uv.lock`. | Plain pip has no lockfile; CI and the container build must resolve identically. |

Deferred by design: OpenTelemetry SDK (P3), Langfuse (P3), FastAPI (P7), `mcp` (P6). Adding them
earlier means carrying unused surface area through every review.

---

## 6. Claude Usage Contract

All model interaction obeys these rules, enforced in one place —
`llm/anthropic_client.py` — so a review can inspect a single file.

### 6.1 Model policy

| Role | Model | Rationale |
|---|---|---|
| Investigator / orchestrator / RCA | `claude-opus-5` | Root-cause reasoning over correlated multi-source evidence is exactly the reasoning-hard, correctness-sensitive workload this tier exists for. 1M context; $5 / $25 per MTok. |
| Sub-agents, judges, bulk extraction (P2 / P4) | `claude-sonnet-5` or `claude-haiku-4-5` | Only after measurement shows quality holds. A cheaper model is a measured decision, never a default. |

Model IDs are exact strings with no date suffix. Model selection is configuration, never a
literal at a call site.

### 6.2 Request parameters

- **Thinking:** `thinking={"type": "adaptive", "display": "summarized"}`. Adaptive thinking is on
  by default on Opus 5; we set it explicitly and opt into summarized display so reasoning
  summaries reach traces and become gradeable by the P4 judge. Display does not change billing.
- **Never** send `budget_tokens` — removed on Opus 5, returns a 400.
- **Never** disable thinking. Cost is controlled with `effort`, not by turning reasoning off.
- **Effort:** `output_config={"effort": ...}`. Default `high` for the investigator; tuned per
  route once P4 evals exist. Judges and extraction run lower.
- **`max_tokens`:** 16000 for non-streaming calls. Any call needing more moves to
  `client.messages.stream(...)` with `get_final_message()`.
- **No assistant prefill** — rejected with a 400 on Opus 5. Output shape is controlled with
  structured outputs.
- **Structured output:** constrained via `output_config={"format": {"type": "json_schema",
  "schema": Analysis.model_json_schema()}}`. The P0.6 adapter calls `client.messages.create(...)`
  directly and validates the returned text with `Analysis.model_validate_json(...)` rather than
  `client.messages.parse(..., output_format=Analysis)` - see ADR-0008 for why (the two are
  wire-identical; `create()` gives one function that classifies every response shape, not two).
- **Strict tools:** every tool definition carries `strict: True`, `additionalProperties: False`,
  and a complete `required` list, so tool arguments are schema-valid by construction.
- **Tool inputs are always parsed as JSON**, never string-matched — escaping inside
  `tool_use.input` is not stable across models.

### 6.3 Prompt-cache discipline

Render order is `tools` → `system` → `messages`; any byte change invalidates everything after
it. Therefore:

1. The system prompt is loaded from a versioned file and contains **no timestamps, no run ids,
   no counters**. One explicit `cache_control: {"type": "ephemeral"}` breakpoint sits at its end.
2. Tool schemas are emitted in deterministic (name-sorted) order with stable JSON key ordering.
3. All volatile content — incident brief, ledger state, turn history — goes *after* the
   breakpoint.
4. Mid-run operator instructions are appended as `{"role": "system", ...}` entries in `messages`
   (supported on Opus 5, no beta header) rather than by editing the top-level system prompt, so
   the cached prefix survives. This is also the prompt-injection-safe operator channel (§7.4).
5. `usage.cache_read_input_tokens` is recorded on every call and asserted non-zero in the
   multi-turn integration test. A silent cache miss is a test failure, not a cost surprise.

**P0.6 status:** 1-3 are built (the frozen system prompt in `llm/prompts/investigator_system.md`,
the cache breakpoint in the request, and `ToolRegistry.schemas()`'s deterministic ordering from
P0.4, sent unmodified). Item 4, the mid-run operator-instruction channel, does not exist yet -
there is no operator in the loop to send one. Item 5 is only partly true: `ModelTurn` captures
`input_tokens`/`output_tokens` on every call, but no P0.6 test asserts a live cache hit, since
that requires the real API and a multi-turn exchange with a stable prefix, which the current live
suite (deliberately narrow per §11 below) does not attempt. Both are candidates for whichever
phase first needs a live, multi-turn, cost-sensitive run.

**Review script:** *"Cache hit rate isn't a cost optimization we hope for — it's an invariant we
assert. The system prompt is frozen by construction and the tool list is sorted, so the prefix
is byte-identical across turns, and there's a test that fails if that stops being true."*

### 6.4 Response handling

`stop_reason` is always inspected before content is read:

| `stop_reason` | Handling |
|---|---|
| `end_turn` | Normal completion. |
| `tool_use` | Execute **all** blocks; return **all** results in a single user message. Splitting results across messages degrades future parallel tool use. |
| `max_tokens` / `model_context_window_exceeded` | Terminal in P0.6 (`ModelContractError`, no retry - see ADR-0008). Recoverable-with-bounded-retry, as described here, is deferred to a later phase. |
| `pause_turn` | Not reachable in P0.6 - the one tool offered (`query_warehouse`) is not a long-running server-side tool. Unhandled `stop_reason`s raise `ModelContractError`. |
| `refusal` | Terminal. `stop_details.category` and `.explanation` recorded in the audit journal. |

A failed tool returns a `tool_result` with `is_error: True` — never a dropped block, which would
desynchronize the conversation.

`response._request_id` and the full `usage` object are recorded on every call.

### 6.5 Cost control

Every model call passes through one gateway that enforces the per-run token budget, records
`usage` (input, output, cache-read, cache-creation), and refuses the call when the budget is
exhausted. Per-run cost is computed and stored on the run record.
`client.messages.count_tokens` is used to estimate context size before large calls.

**P0.6 status:** the turn/tool-call/token/deadline budget (`Budget`/`BudgetTracker`, P0.3) is
enforced unchanged by the agent loop and is what makes every run bounded (I5); no new budget
mechanism was introduced. A transient model error (rate limit, connection failure, 5xx) ends the
run rather than retrying, and `client.messages.count_tokens` pre-flight estimation is not yet
used - both are scope cuts recorded in ADR-0008, not gaps discovered by accident.

---

## 7. Security and Authorization Model

### 7.1 Agent identity

Every agent instance carries an `AgentIdentity`: a stable `agent_id`, a `role`, and a set of
granted `Permission` values. Identity is constructed at run start from configuration, is
immutable for the life of the run, and is stamped onto every evidence record and audit entry.

**Why:** requirement 19. Without identity, "which agent did that" is unanswerable — and sub-agent
decomposition (P2) silently becomes privilege escalation, with a research sub-agent inheriting
the orchestrator's write authority.

### 7.2 Tool permissions

Each tool declares `name`, `permission` (the capability required to invoke it), `mutating`
(bool), and `risk` (low / medium / high). The executor enforces, in order:

1. **Authorization** — is `tool.permission` in `identity.permissions`? Deny → an error
   `tool_result`, an audit entry, and the run continues. A denial is data, not a crash.
2. **Mutation gate** — if `tool.mutating`, an unexpired, scope-matching approval token must be
   present. Absent → denied. Phase 0 registers no mutating tool; the gate is proven with a
   test-only mutating tool that must be denied.
3. **Input validation** — arguments validated against the tool's schema before execution.
4. **Execution** — inside a timeout, with typed errors.
5. **Recording** — evidence written, audit entry appended.

### 7.3 Data-access safety

`query_warehouse` (P0.5) enforces three independent layers, none relied on alone: statement-shape
validation against DuckDB's own parser (exactly one statement, classified as `SELECT`, and —
closing a gap the parser's own classification does not — beginning with the literal keyword
`SELECT` or `WITH`); a connection opened read-only with external file/extension access disabled
at the engine; and bounded execution — a row cap enforced via incremental fetch (not post-hoc
truncation), a result-byte cap, and a timeout backed by DuckDB's own query cancellation
(`interrupt()`), which is a tested, genuine abort rather than the abandon-only fallback described
in §8's error taxonomy. Query text is recorded verbatim in the evidence record. Every claim above
was verified empirically against DuckDB 1.5.5, including two gaps found and closed during
implementation — see ADR-0007 for the full basis, the alternatives rejected, and the precise
limitations.

### 7.4 Prompt-injection posture

Retrieved evidence — log lines, commit messages, dbt failure text — is **untrusted input**. It
enters the conversation as tool results, never as system-level instruction, and is never
concatenated into the system prompt. Operator instructions arriving mid-run use the
`role: "system"` message channel, which carries operator authority and is the injection-safe
path. Tool authorization is enforced in Python against an identity fixed before the run started,
so nothing the model reads can grant a permission.

**Review script:** *"An investigation agent reads attacker-influenceable text by definition — a
commit message, a log line. So evidence never enters the instruction channel, and no amount of
text in a tool result can grant a permission, because permissions are checked in code against an
identity fixed before the run began."*

### 7.5 Secrets

No secret is ever committed. Configuration comes from environment variables validated at
startup; `.env.example` documents the surface with placeholders. `ANTHROPIC_API_KEY` is read by
the SDK from the environment and is never logged, never included in audit records, and never
rendered in error output.

---

## 8. Failure Handling and Error Taxonomy

Errors are typed and classified by recoverability. This is what makes requirement 22
implementable rather than aspirational.

```
CausiqError
├── ConfigurationError          fatal at startup, never at runtime
├── DomainError                 invariant violation (e.g. unresolvable citation) — fatal to run
├── ModelError
│   ├── ModelTransientError     429 / 5xx / connection / timeout → SDK retries, then we do
│   ├── ModelRefusalError       stop_reason == "refusal"         → terminal, recorded
│   └── ModelContractError      schema-invalid output            → one repair attempt, then fail
├── ToolError
│   ├── ToolAuthorizationError  denied                           → recoverable, returned to model
│   ├── ToolExecutionError      the tool itself failed           → recoverable, returned to model
│   └── ToolTimeoutError                                         → recoverable, returned to model
└── BudgetExceededError         turns / tokens / deadline        → run ends INCONCLUSIVE, cleanly
```

Rules:

- **Recoverable tool failures never crash the run.** They become `is_error` tool results so the
  model can adapt — which is the entire point of an investigating agent.
- **Every run terminates in a persisted terminal state:** `COMPLETED`, `INCONCLUSIVE`, `DENIED`,
  `BUDGET_EXCEEDED`, or `FAILED`. Even a crashed process leaves an audit journal that
  reconstructs how far it got.
- **Retry is bounded and layered.** The SDK retries transport-level failures (default 2); above
  that we retry only classified transient errors, with jittered backoff and a hard cap.
- **No bare `except`.** Exception handling is a most-specific-first chain against the SDK's typed
  exceptions (`NotFoundError` → `RateLimitError` → `APIStatusError` → `APIConnectionError`);
  collapsing them loses the retryable / non-retryable distinction.

---

## 9. Observability Contract

Phase 0 delivers the seam and structured logging; Phase 3 delivers OTel and Langfuse.

- **Logging:** structlog, JSON output, with `run_id`, `incident_id`, and `agent_id` bound to
  every line from run start.
- **Span vocabulary (fixed now, emitted in P3):** `causiq.run`, `causiq.agent.turn`,
  `causiq.model.call`, `causiq.tool.authorize`, `causiq.tool.execute`, `causiq.evidence.record`,
  `causiq.analysis.validate`. Naming them now means Phase 3 adds an adapter, not call sites.
- **Per model call:** model id, effort, input / output / cache-read / cache-write tokens,
  latency, `stop_reason`, `_request_id`.
- **Per tool call:** tool name, identity, authorization decision, duration, outcome, evidence id,
  result byte size.
- **Langfuse (P3)** consumes traces for prompt- and version-level analysis and becomes the input
  surface for P4 evaluation. It is an exporter, not a dependency of the agent loop.

---

## 10. Testing Contract

| Tier | Scope | Speed | Runs in CI | Needs API key |
|---|---|---|---|---|
| **unit** | Pure logic: domain models, budget arithmetic, authorization decisions, context assembly, citation validation | ms | always | no |
| **contract** | Schema stability, tool-surface stability, prompt-file checksums, error-taxonomy completeness | ms | always | no |
| **integration** | Real DuckDB, real fixtures, full loop with `FakeModelClient`; plus a small live-API suite | s | always (live suite skipped without a key) | only the live suite |
| **eval** (P4) | Scored agent behaviour: correctness, tool-use quality, multi-turn coherence, judge scores | min | nightly / on demand | yes |

Rules:

- The default `make test` runs offline, deterministically, with no API key and no network.
- Live-API tests are marked and **skipped** when `ANTHROPIC_API_KEY` is absent — never silently
  passing, always visibly skipped.
- Coverage gate on `src/causiq/` starting at 85%, with the domain layer, the authorization path,
  the citation validator, and the capability layer (`tools/`) required at 100%. Those four are
  where the invariants are enforced; an uncovered branch in one of them is an unenforced rule.
  Coverage is a floor, not a goal.
- Every bug fix begins with a failing test.
- Determinism: the `Clock` port and seeded ids mean two runs of the offline suite produce
  byte-identical audit journals. Non-determinism in tests is treated as a defect.

**Review script:** *"The suite runs offline because the model client is a port. That's not a
testing trick — it means the parts of an AI system that must be correct are tested
deterministically, and the expensive non-deterministic testing is reserved for the part that is
genuinely non-deterministic: the model's judgement. That gets scored in the eval tier, not
asserted in unit tests."*

---

## 11. Definition of Done and Maturity Labels

Every component carries an explicit maturity label in its module docstring and in the docs.

| Label | Means |
|---|---|
| `experimental` | Works on the happy path. Not to be relied on. |
| `hardened` | Typed, tested, error-handled, configured, documented. |
| `production-ready` | `hardened` **plus** observability, failure/recovery behaviour proven by test, an operational runbook, and a deployment story. |

**A component may not be described as production-ready until every box is ticked:**

- [ ] Typed; `mypy --strict` clean
- [ ] Unit and contract tests covering failure paths, not just the happy path
- [ ] All error modes classified in the taxonomy and handled explicitly
- [ ] Configuration externalized and validated at startup
- [ ] Structured logging with correlation ids; span names defined (emitted from P3)
- [ ] Bounded: timeouts, retry caps, resource limits
- [ ] Documented: purpose, interface, failure modes, how to operate it
- [ ] Reviewed against the invariants in §2

This checklist is the answer to "is it done?" — and the reason nothing in Phase 0 will be called
production-ready before Phase 3 lands observability.

---

## 12. Requirement Traceability

All 28 required capabilities, mapped to the phase that delivers them. No capability is orphaned;
none is built before it has a real consumer.

| # | Capability | Phase | Notes |
|---|---|---|---|
| 1 | Claude-based AI agents | **P0** | Single investigator agent, real API call. |
| 2 | Agent orchestration | P2 | Orchestrator over specialized agents. |
| 3 | Sub-agent decomposition | P2 | Pipeline / data-quality / code-change specialists. |
| 4 | Context engineering | **P0** → P2 | Cache discipline and budget in P0; ledger summarization, context editing, compaction in P2. |
| 5 | A2A communication | P2 → P6 | Typed message envelopes in-process (P2); network transport (P6). |
| 6 | MCP / tool integration | **P0** → P6 | Native tool use in P0; Causiq as MCP client and MCP server in P6. |
| 7 | Software incident investigation | P1 | Git, deploys, logs. |
| 8 | Data engineering / DQ investigation | **P0** → P1 | Warehouse queries in P0; dbt, Airflow, DQ results, schema history in P1. |
| 9 | Evidence-backed RCA | **P0** | Invariant I1 plus the citation validator, enforced from day one. |
| 10 | Remediation planning | P5 | |
| 11 | Human-in-the-loop approval | P5 | The gate exists and is tested in P0 (I4); there is no mutating tool to gate until P5. |
| 12 | Post-remediation verification | P5 | |
| 13 | OpenTelemetry tracing | P3 | Span vocabulary fixed in P0. |
| 14 | Langfuse observability | P3 | |
| 15 | LLM-as-a-Judge | P4 | |
| 16 | Agent / workflow evaluation | P4 | |
| 17 | Multi-turn evaluation | P4 | |
| 18 | Tool-use evaluation | P4 | |
| 19 | Agent identity and authorization | **P0** | Identity object and permission set from the first commit. |
| 20 | Tool permission controls | **P0** | Enforced in the executor; the denial path is tested. |
| 21 | Auditability | **P0** → all | Append-only journal in P0; durable store in P7. |
| 22 | Failure handling and recovery | **P0** → all | Taxonomy and bounded runs in P0; resumability in P5. |
| 23 | Docker deployment | P7 | |
| 24 | AWS serverless deployment | P7 | |
| 25 | ECS deployment | P7 | |
| 26 | Kubernetes deployment | P7 | |
| 27 | Automated testing | **P0** → all | Test contract §10 from the first commit. |
| 28 | Production engineering practices | **P0** → all | §11 checklist. |

### Phase roadmap

| Phase | Deliverable | Exit condition |
|---|---|---|
| **P0** | Walking skeleton: one incident, one agent, one tool, one real Claude call, a validated evidence-backed finding, an audit trail, CI | Every acceptance criterion in `PHASE_0_PLAN.md` passes |
| **P1** | Full evidence surface: dbt, Airflow, git, deploys, schema history, DQ results; multi-source correlation on an incident timeline | Agent solves 3 distinct seeded incident classes with correct, cited root causes |
| **P2** | Orchestrator, specialist sub-agents, typed A2A envelopes, context management under a growing ledger | Multi-agent run beats the single-agent baseline on the P1 incident set |
| **P3** | OpenTelemetry tracing and Langfuse | A complete run is inspectable as a trace tree with token, cost, and latency per span |
| **P4** | Evaluation harness: golden set, LLM-as-a-Judge, tool-use and multi-turn scoring, CI regression gate | Scores are reproducible and a prompt regression fails the build |
| **P5** | Remediation planning, approval workflow, gated execution, verification, run resumability | An approved remediation executes, is verified, and is fully auditable |
| **P6** | MCP server and client, network A2A, authorization hardening | An external MCP client can invoke Causiq tools under scoped authorization |
| **P7** | Docker → ECS → Kubernetes → serverless; durable persistence | The same image runs on all targets from one entrypoint |

---

## 13. Working Agreement

- **Methodology:** PLAN → IMPLEMENT → TEST → REVIEW → DOCUMENT → COMMIT → NEXT MILESTONE. No
  phase begins before the previous phase's acceptance criteria pass.
- **Scope discipline:** if a component cannot name the requirement it serves and the phase that
  needs it, it does not get built. This rule is what keeps the architecture explainable.
- **ADRs:** any decision that would be expensive to reverse gets a numbered file in `docs/adr/`
  with context, decision, alternatives, and consequences.
- **Commits:** conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`), scoped to one
  logical change, with a green test suite.
- **Documentation is part of the definition of done**, not a follow-up task.
- **Reversal policy:** any decision here can be revisited. Revisiting it means writing the ADR
  that supersedes it, not quietly diverging in code.
