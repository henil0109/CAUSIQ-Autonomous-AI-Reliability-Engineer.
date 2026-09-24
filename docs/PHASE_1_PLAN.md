# Causiq — Phase 1 Evidence Surface Plan

**Phase:** P1 — Full Evidence Surface
**Status:** Approved — P1.1 complete; P1.2 onward not yet started
**Depends on:** `ENGINEERING_CONTRACT.md` v1.0, `PHASE_0_PLAN.md` (Phase 0 complete, P0.1–P0.7)
**Last updated:** 2026-09-24

---

## 1. The Phase 1 Thesis

Phase 0 proved the architecture on one incident, one tool, one evidence source. That was a
deliberate simplification, not a design ceiling: `EvidenceSource`, `Permission.ARTIFACTS_READ`,
`Hypothesis`, and the `Tool` protocol were all already shaped in P0.3/P0.4 to carry more sources
without changing. Phase 1 exercises that headroom for real, by adding the evidence surface a real
data/software reliability investigation actually needs - Airflow, dbt, git, deployment records,
schema history, data-quality results - and by seeding three *distinct* incident classes so a
single incident's prompt can no longer be quietly overfit.

**Review script:** *"Phase 0 answers 'can this system make a claim it cannot back up?' Phase 1
answers a harder question: does the architecture that enforces that actually generalize past the
one tool and one incident it was proven on, or did P0 quietly bake in warehouse-shaped
assumptions? P1.1 is the test of that - a second, differently-shaped evidence source, through the
same unmodified machinery."*

## 2. P1 Exit Criterion

Per `ENGINEERING_CONTRACT.md` §12 (Requirement Traceability, Phase roadmap):

> Agent solves 3 distinct seeded incident classes with correct, cited root causes.

"Solves" means: the agent reaches `RunState.COMPLETED`, its `Analysis.root_cause` matches the
seeded (never-encoded-as-data) true cause, and every citation resolves against real evidence
collected that run — the same bar `INC-001` already met in Phase 0, applied three times, across
different evidence combinations.

## 3. The Three Seeded Incident Classes

None of these is implemented yet (P1.1 seeds no incident at all — see §6). This section fixes the
target shape so later milestones build toward a known, reviewed design rather than improvising
per-milestone.

Across all three, the Phase 0 discipline holds unchanged: fixtures plant **facts only** — no field
named `root_cause`, no causal conclusion encoded as data. The true cause must be a *derivable
relationship between planted facts* (a timestamp alignment, a status change, a query result),
exactly like `INC-001`'s `revenue_daily` filter defect.

### Class A — Pipeline / freshness failure

- **Symptom:** a warehouse table has not refreshed on schedule; no error is visible in application
  logs.
- **Evidence combination:** Airflow (a task failed or never ran) → dbt (the dependent model run is
  absent or skipped) → warehouse (the table's max timestamp/partition is frozen) → DQ (a freshness
  check fires).
- **Causal shape:** an *absence* — something that should have run did not, and the consequence is
  observed two systems downstream.
- **Distinguishing skill:** noticing a negative (nothing happened) and connecting it across
  systems that are never directly compared by default.

### Class B — Deployment / schema incompatibility

- **Symptom:** writes into a table begin failing (or silently corrupting) starting at a specific
  time, with no upstream volume change.
- **Evidence combination:** deployment record (pins the timestamp) → git commit (names the
  schema-relevant change) → schema history → warehouse evidence (failure onset aligned to the
  deploy timestamp).
- **Causal shape:** a *hard failure* with a precise timestamp anchor across systems that have no
  Airflow/dbt involvement at all.
- **Distinguishing skill:** precise temporal correlation — the failure must align with the exact
  deploy time, not merely "some code changed at some point."

### Class C — Transformation / data-quality regression

- **Symptom:** a model's values are wrong; the model's own scheduled runs report success.
- **Evidence combination:** git commit (a transformation-logic change) → dbt (the run
  *succeeded* — a ruling-out citation, not a confirming one) → DQ (a reconciliation/bounds check
  fails) → warehouse (the discrepancy, independently re-derivable — the same "re-execute by hand"
  property as `INC-001`).
- **Causal shape:** *silent* — nothing fails at the pipeline level; only a downstream check and
  the data itself reveal the regression.
- **Distinguishing skill:** citing evidence that rules a cause out (the successful dbt run) as
  part of the argument, and tracing a symptom back to a specific commit rather than stopping at
  "the logic is wrong."

Each class exercises a different failure signature (absence / hard failure / silent regression)
and a different evidence combination, so solving all three is evidence the architecture
generalizes rather than evidence the agent memorized one shape.

## 4. Evidence Sources

| Source | `EvidenceSource` member | Status | Tool |
|---|---|---|---|
| Warehouse | `WAREHOUSE` | Delivered, P0.5 | `query_warehouse` |
| Airflow | `AIRFLOW` | **Delivered, P1.1** | `query_airflow_runs` |
| dbt | `DBT` | Deferred | P1.2 |
| Git | `GIT` | Deferred | P1.4 |
| Deployment records | `DEPLOYMENT` | Deferred | P1.4 |
| Data-quality results | `DATA_QUALITY` | Deferred | P1.3 |
| Schema history | *(none yet — may reuse `WAREHOUSE`)* | Deferred | Decide at P1.5: likely additional DuckDB tables under `query_warehouse`, not a sixth tool (§8) |

Every source is a `Tool` (ADR-0009). `Permission.ARTIFACTS_READ` is the one shared read
permission across every non-warehouse source — sources are attributed by `EvidenceSource`/tool
name, not by a separate permission each.

## 5. Milestone Sequence

| Milestone | Adds | Unlocks |
|---|---|---|
| **P1.1** | Airflow tool + artifact-fixture substrate + ADR-0009 (**this milestone, complete**) | Proves the pattern generalizes past the warehouse |
| P1.2 | dbt `run_results.json` tool | Second source; still no full incident wired |
| P1.3 | DQ-results tool + warehouse fixture extension (freshness columns) + `INC-002` + integration test | First of the 3 required incident-class proofs (Class A) |
| P1.4 | Git-log tool + deployment-records tool | Enables Class B |
| P1.5 | `INC-003` fixtures + integration test; decide whether schema history needs a new tool or reuses `query_warehouse` | Second proof (Class B) |
| P1.6 | `INC-004` fixtures + integration test, reusing P1.2/P1.4 tools | Third proof (Class C) — P1 exit criterion met |
| P1.7 | Cross-incident regression suite, this document finalized, ADR consolidation, final P1 acceptance review | Phase close |

Each milestone is scoped to be independently reviewable and committable, matching the discipline
Phase 0's sub-phases (P0.1–P0.7) already established.

## 6. P1.1 Scope

**Delivered:** evidence-source contract proof + deterministic fixture framework + one
representative Airflow source adapter.

**What P1.1 deliberately does not do:**

- Wire a complete seeded incident. `INC-002` (Class A) needs dbt and DQ evidence too — that is
  P1.3.
- Add a second source (dbt, git, deployment, DQ).
- Introduce any shared "artifact tool base class" — see ADR-0009's "Alternatives considered" and
  the guard-against-drift note: extracted only once a second tool would otherwise duplicate real
  code.
- Touch `Investigator`, `ToolExecutor`, `authz/decision.py`, `evidence/ledger.py`,
  `audit/journal.py`, or any P0 security invariant. `runner.py` gained exactly one line
  registering the new tool.

### Files delivered

- `fixtures/artifacts/airflow/dag_runs.json` — static, hand-authored, deterministic (fixed
  timestamps, no `RANDOM()`/`NOW()`/`UUID()`-equivalent, no `root_cause` field).
- `src/causiq/artifact_substrate.py` — fixture loading, pydantic validation, in-memory
  `AirflowDagIndex`.
- `src/causiq/tools/airflow.py` — `AirflowRunsInput`, `AirflowDagRunsTool` (`query_airflow_runs`).
- `src/causiq/domain/enums.py` — `EvidenceSource.AIRFLOW` added; `DBT`/`GIT`/`DEPLOYMENT`/
  `DATA_QUALITY` remain undeclared.
- `src/causiq/runner.py` — `AirflowDagRunsTool()` registered alongside `QueryWarehouseTool` in
  `build_registry()`.
- `tests/unit/test_artifact_substrate.py`, `tests/unit/test_airflow_tool.py`,
  `tests/integration/test_airflow_evidence.py` — substrate determinism/rejection, tool contract
  and input-safety, and real-executor evidence/authorization/failure behavior respectively.
- `docs/adr/0009-artifact-evidence-sources.md` — the design record.

## 7. P1.1 Acceptance Criteria

- **P1.1-AC-1** `query_airflow_runs` is registered; its emitted schema is byte-stable across calls
  (`ToolRegistry.schema_digest()` unchanged across two calls), and adding it does not change the
  warehouse tool's own emitted schema.
- **P1.1-AC-2** An authorized call for a real `dag_id` produces a verified `Evidence` record with
  `source == EvidenceSource.AIRFLOW`, correct `agent_id`, and the original request/response
  preserved verbatim.
- **P1.1-AC-3** An identity lacking `ARTIFACTS_READ` is denied (`TOOL_DENIED`, audited), and
  produces no evidence.
- **P1.1-AC-4** A path-traversal- or absolute-path-shaped `dag_id` is rejected at the input-schema
  level (`ToolResultStatus.INVALID_INPUT`), never reaching `run()`.
- **P1.1-AC-5** An unknown but well-formed `dag_id` fails cleanly (`ToolResultStatus.FAILED`,
  audited, no evidence) — no new error taxonomy member.
- **P1.1-AC-6** Fixture loading is deterministic (two loads produce byte-identical content), and
  malformed JSON / malformed structure / naive timestamps / unknown states / duplicate `dag_id`s
  are all rejected at load time.
- **P1.1-AC-7** Coverage gates hold: ≥85% overall, 100% on `domain/`, `evidence/`, `authz/`,
  `tools/` (including the new `tools/airflow.py`).
- **P1.1-AC-8** `ruff`/`mypy --strict` clean; no new third-party dependency.
- **P1.1-AC-9** Every pre-existing Phase 0 test continues passing unmodified; the offline suite
  remains network-free and API-key-free by default.

## 8. Explicit Deferrals

Listing these is as important as the scope itself — each is deferred for a stated reason.

| Not building yet | Why not yet |
|---|---|
| dbt, git, deployment, DQ tools | P1.2–P1.4. One source proves the pattern; the rest follow it without re-litigating the design. |
| A shared artifact-tool base class | No second concrete tool exists yet to validate its shape against (ADR-0009). |
| `INC-002`/`INC-003`/`INC-004` | Each needs evidence sources that don't exist yet. |
| A per-source `Permission` (e.g. `AIRFLOW_READ`) | No security distinction it would buy yet; revisit at P5 if mutating remediation tools change the calculus. |
| A sixth tool for "schema history" | May be additional `query_warehouse` tables instead — decide at P1.5 with the actual need in front of us, not speculatively now. |
| Multi-agent orchestration, A2A, MCP | P2/P6. |
| OpenTelemetry, Langfuse exporters | P3. The `Tracer` port and span vocabulary already exist (P0.4); only the exporter is deferred. |
| Evaluation harness, LLM-as-Judge | P4. |
| Remediation planning, approval workflow execution | P5. The approval *gate* already exists and is tested (P0, AC-8). |
| Docker/ECS/Kubernetes | P7. |

## 9. Security Invariants Preserved

Confirmed by inspection, not merely asserted (`git diff --stat` shows none of these files changed
by P1.1):

- **I1 — no claim without evidence.** `validate_citations`/`require_valid_citations` unchanged;
  Airflow evidence resolves exactly like warehouse evidence.
- **I2 — evidence is immutable and attributable.** `Evidence`/`EvidenceRequest`/`digest`/`verify()`
  unchanged; Airflow evidence is verified the same way.
- **I3 — the model never touches a system directly.** `AirflowDagRunsTool` is reached only through
  `ToolRegistry`/`ToolExecutor`, exactly like every other tool.
- **I4 — read is default; write is granted and requires human approval.**
  `AirflowDagRunsTool.spec.mutating` is `False`; no P1.1 tool is mutating, so the approval gate
  (already proven in P0, AC-8) has nothing new to gate yet.
- `ToolExecutor` remains the single tool-execution boundary; `authz.authorize` remains the single
  authorization decision point; `EvidenceLedger` remains the only evidence-creation path;
  citation validation, budget enforcement, and the audit lifecycle are all unchanged.
- No source adapter may, and none does, bypass any of the above — `AirflowDagRunsTool.run()`
  touches only its own in-memory fixture index, never the ledger, the journal, or the filesystem
  based on model input (ADR-0009).

---

## 10. Definition of Done for P1.1

- [x] `AirflowDagRunsTool` registered and reachable via `runner.build_registry()`
- [x] All P1.1-AC-1 … AC-9 pass
- [x] ADR-0009 committed
- [x] This document committed
- [x] Every pre-existing Phase 0 test still passes, unmodified
- [x] No P0 security-relevant module (`agents/`, `tools/executor.py`, `tools/registry.py`,
      `authz/`, `evidence/`) touched
- [ ] Reviewed and committed (pending explicit review sign-off per project process)

Only then does P1.2 planning begin.
