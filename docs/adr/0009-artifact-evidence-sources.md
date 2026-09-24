# ADR-0009 — Artifact evidence sources: one Tool per source, no new abstraction layer

**Status:** Accepted
**Date:** 2026-09-24
**Requirements served:** 7 (software incident investigation), 8 (data engineering / DQ
investigation, P1 half), 9 (evidence-backed RCA)

## Context

Phase 0 has exactly one evidence source (`query_warehouse`, ADR-0007). Phase 1's exit criterion
is solving three distinct seeded incident classes, which requires evidence from systems the
warehouse tool cannot see: Airflow, dbt, git, deployment records, and data-quality results
(`EvidenceSource`'s own docstring reserved these names since P0.3, deliberately undeclared until
their tools existed). P1.1 is the first slice: prove that a *non-warehouse* source can flow
through the existing P0 machinery - `Investigator`, `ToolExecutor`, `authz`, `EvidenceLedger`,
`AuditJournal`, citation validation - without changing any of it, using Airflow as the
representative case.

## Decision

**Each evidence source sits behind its own `Tool` implementation.** This is not a new decision;
it restates ADR-0003's integration path ("each evidence source sits behind a tool... swapping the
implementation is a new adapter behind the same tool contract; the agent layer does not change")
and applies it to a source that isn't the warehouse. Concretely, for P1.1:

- `src/causiq/tools/airflow.py` — `AirflowRunsInput(ToolInput)` + `AirflowDagRunsTool`, structured
  exactly like `causiq.tools.warehouse.WarehouseQueryInput`/`QueryWarehouseTool`: a `ToolSpec`
  declaring `permission`, `mutating`, `risk`, `source`, `timeout_seconds`; a typed input model; a
  `run()` that returns `ToolOutput` and touches nothing else.
- `src/causiq/artifact_substrate.py` — the fixture-loading counterpart to
  `causiq.evidence_substrate`, but for a static, already-committed JSON file rather than a
  database that must be *built*. Owns exactly three things: locate the fixed fixture path, parse
  and pydantic-validate it, and build an in-memory `AirflowDagIndex`.
- `EvidenceSource.AIRFLOW` — the only new domain change. `DBT`, `GIT`, `DEPLOYMENT`, and
  `DATA_QUALITY` remain undeclared until their own tools land, exactly as the enum's docstring has
  said since P0.3.
- `Permission.ARTIFACTS_READ` — already declared in P0.3, unused until now. Every P1 artifact
  source shares this one permission; sources are distinguished by `EvidenceSource`/tool name for
  attribution, not by a separate permission each. A read is a read; the security-relevant
  distinction P0 already draws is read vs. write (`WAREHOUSE_READ`/`WAREHOUSE_WRITE`), not
  warehouse vs. Airflow vs. dbt.

**Explicitly not introduced:** an `ArtifactTool` base class, a `SourceAdapter` framework, a
`SourceRegistry`, an `EvidenceSourceManager`, or any generic artifact abstraction layer. The
existing `Tool` protocol (`tools/base.py`) already *is* the source contract — it says nothing
warehouse-specific, and `ToolExecutor`/`ToolRegistry`/`authz.authorize`/`EvidenceLedger` already
treat it as the only contract that matters. A base class or framework with exactly one concrete
user (`AirflowDagRunsTool`) would be premature generality with no second user to validate its
shape against - the same reasoning P0.6 used to defer a shared `ArtifactFixtureTool` mixin until a
second source tool exists to justify it. When P1.2 adds a second source, whatever genuinely
duplicates between the two tools gets extracted then, informed by two real implementations rather
than a guess.

### The filesystem-path-injection guard

The one security property this ADR states explicitly, because it is the one genuinely new risk a
file-backed (rather than SQL-backed) tool introduces: **a model-supplied string must never become
a filesystem path.** Two independent layers enforce it, neither alone sufficient:

1. **Structural.** `AirflowDagIndex` is built once, at tool construction, from
   `DEFAULT_AIRFLOW_FIXTURE_PATH` - a constant, never a value derived from a request. Every
   subsequent lookup (`AirflowDagRunsTool.run()` → `AirflowDagIndex.get(dag_id)`) is a plain
   `dict.get`. There is no line of code, anywhere in this path, that joins, formats, or resolves a
   `Path` from `dag_id`. A hostile string cannot reach the filesystem because there is no code path
   by which any string reaches the filesystem after construction.
2. **Schema-level, defense in depth.** `AirflowRunsInput.dag_id` is constrained by
   `causiq.tools.airflow.DAG_ID_PATTERN` (`^[A-Za-z0-9_-]{1,200}$`) - excluding `/`, `\`, `.`, and
   whitespace outright, stricter than real Airflow's own naming rules. This means a path-traversal
   or absolute-path-shaped argument is rejected by `ToolExecutor`'s existing input-validation gate
   (`ToolResultStatus.INVALID_INPUT`) before `run()` is ever called - the same "reject before
   execution" discipline `sql_policy.py` applies to SQL, applied here to a simpler, closed
   identifier space instead of a query language.

An **unknown but well-formed** `dag_id` (one that matches the pattern but isn't in the fixture) is
a different, ordinary case: `AirflowDagIndex.get()` returns `None`, `run()` raises the existing
`causiq.errors.ToolExecutionError`, and `ToolExecutor` classifies it as `ToolResultStatus.FAILED` -
audited, no evidence recorded. No new error taxonomy member exists or is needed; this is exactly
the path a failed `query_warehouse` call already takes.

### Cross-source correlation without changing `Evidence`

P1's incident classes require reasoning about *when* a fact occurred in the source system (a task's
execution date), not merely when Causiq collected it (`Evidence.collected_at`). Rather than adding
a field to the hardened, 100%-covered `Evidence` model, each source's own `content` carries its
own event timestamps - `AirflowDagRun.execution_date`/`start_date`/`end_date` are already present
in every Airflow evidence record's JSON content, exactly as `raw.orders.order_ts` already is inside
`query_warehouse` results. The model correlates timing by reading data it already has, the same way
it already does for the warehouse. `Evidence`, `EvidenceRequest`, `Hypothesis`, `Incident`, and
`Analysis` all required zero changes for P1.1 - confirmed by inspection before this ADR was written,
not assumed.

## Alternatives considered

**A generic `ArtifactSource` protocol distinct from `Tool`, with tools adapting to it.** Rejected:
it would duplicate `Tool`'s existing responsibilities (permission, schema, evidence source tag) for
no behavioral gain, and `ToolExecutor` would need to know about two contracts instead of one.

**A per-source `Permission` (`AIRFLOW_READ`, `DBT_READ`, ...).** Rejected for now: P1's artifact
sources carry identical risk (read-only, low-risk metadata), so a finer-grained permission buys no
security distinction yet, only more enum members to keep in sync across every future read-only
tool. Worth revisiting once P5's mutating remediation tools exist alongside these and a real case
for finer read scopes appears.

**Loading the fixture per-call instead of once at construction.** Rejected: there is no live system
to poll - the fixture is static and committed - so re-reading it on every call would be pure
overhead with no correctness benefit, and would reopen the (structurally already-closed) question
of whether a call-time value could ever influence which file gets read.

## Consequences

**Positive.** `Investigator`, `ToolExecutor`, `authz.decision`, `evidence.ledger`, and
`audit.journal` needed zero changes - confirmed by `git diff` showing none of those files touched.
A second source tool (P1.2+) follows this same pattern with no architectural decision left to
relitigate.

**Negative.** Every new source needs its own small loader module (`artifact_substrate.py`'s
pattern, not yet shared code) until a second concrete tool justifies extracting the common parts.
This is an accepted, deliberate cost - see "Alternatives considered."

**Guard against drift.** If a second artifact tool (P1.2) finds itself copy-pasting real logic from
`artifact_substrate.py` rather than merely following its shape, that is the trigger to extract a
shared loader - not before.

## How we test it

`tests/unit/test_artifact_substrate.py` - fixture loading, determinism (two loads produce
byte-identical content), and rejection of malformed JSON, malformed structure, naive timestamps,
unknown states, and duplicate `dag_id`s. `tests/unit/test_airflow_tool.py` - the tool's own
contract: valid lookup, deterministic output, unknown-DAG failure, and a parametrized set of
path-traversal/absolute-path/malformed `dag_id` shapes proving schema-level rejection, plus an
empirical test that severs `open()` entirely and proves both a successful and a failing lookup
never touch it. `tests/integration/test_airflow_evidence.py` - the same three cases
(`test_inc001_agent_investigation.py`'s pattern) through the real `ToolExecutor`/`authz`/
`EvidenceLedger`/`AuditJournal`, plus a coexistence test proving the warehouse and Airflow tools
share one registry cleanly and a schema-stability test proving adding a second tool doesn't change
the first tool's own emitted schema or break `ToolRegistry.schema_digest()`'s determinism.

## Review script

*"Airflow is a `Tool`, exactly like the warehouse - nothing downstream of the registry knows or
cares that this one reads a JSON file instead of a SQL database. The one new risk a file-backed
tool introduces - a model turning a string into a path - is closed twice over: there's no code path
that ever builds a path from the model's input at all, and the input schema rejects anything
path-shaped before that code would even run."*
