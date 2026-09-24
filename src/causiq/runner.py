"""The CLI-facing runner - the thin operator layer around `Investigator` (P0.7).

Maturity: hardened.

This module owns exactly four things: choosing a `ModelClient` (fake for
`--dry-run`, the real Anthropic adapter otherwise), assembling the same tool
registry and agent identity every investigation uses, invoking the *existing*
`Investigator` with the *existing* `Budget`/`Settings` abstractions, and
persisting the result. It does not decide authorization, does not touch
DuckDB, and does not read or write the evidence ledger - `Investigator`
already owns all of that, and this module is not permitted to duplicate or
bypass it (the same boundary P0.6's own module docstring states, restated
here because a runner is exactly the kind of layer that accretes shortcuts).

**AC-9 and the persistence boundary.** `InvestigationRun` (P0.3) already
carries the run's full evidence (`ledger.snapshot()`, threaded through
unchanged since P0.6) alongside its terminal state, budget usage, and
analysis - so "persist a partial ledger" does not require a new domain model
or a new field, only somewhere to write the record that already exists. This
module writes two files per run, both under `Settings`-configured
directories:

* `{audit_dir}/{run_id}.jsonl` - the append-only audit journal
  (`causiq.audit.JsonlAuditSink`, unchanged from P0.2/P0.4), flushed after
  every entry, so it survives a crash mid-run.
* `{runs_dir}/{run_id}.json` - the terminal `InvestigationRun` record,
  written once, after `Investigator.investigate()` returns. Because
  `investigate()` always returns (never raises) for every bounded outcome -
  `COMPLETED`, `INCONCLUSIVE`, `BUDGET_EXCEEDED`, or `FAILED` - this single
  write path persists the partial ledger for *every* terminal state alike,
  including a budget-exceeded run: no special-casing is needed because
  `InvestigationRun.evidence` already contains whatever was collected before
  the budget stopped the loop.

**Known limitation, stated plainly.** This is deliberately not a database or
a write-ahead log: if the process is killed between `investigate()` returning
and the result-file write completing, the audit journal (already flushed
incrementally throughout the run) survives, but the single-file result
record does not. That is an acceptable Phase 0 boundary - the journal alone
is sufficient to reconstruct what happened (invariant I7) - and is exactly
the kind of gap a real persistence layer would close in a later phase, not
something P0.7 needs to solve.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from causiq.agents import Investigator
from causiq.audit import JsonlAuditSink
from causiq.authz import AgentIdentity, Permission
from causiq.clock import Clock, SystemClock
from causiq.config import Settings, load_settings
from causiq.domain import (
    AgentRole,
    Analysis,
    AnalysisOutcome,
    Claim,
    Confidence,
    Incident,
    InvestigationRun,
)
from causiq.evidence_substrate import (
    DEFAULT_WAREHOUSE_PATH,
    build_warehouse_db,
    load_incident_by_id,
)
from causiq.ids import EvidenceId, IdGenerator, RunId, Uuid4IdGenerator
from causiq.llm.anthropic_client import AnthropicModelClient
from causiq.llm.fake_client import FakeModelClient, ScriptEntry
from causiq.llm.ports import (
    ConversationMessage,
    ModelClient,
    ModelTurn,
    ToolCallBlock,
    ToolResultBlock,
)
from causiq.tools import ToolRegistry
from causiq.tools.airflow import AirflowDagRunsTool
from causiq.tools.warehouse import QueryWarehouseTool

#: The identity every CLI-driven investigation runs under. One fixed identity
#: is sufficient for Phase 0 (a single agent, a single read permission); a
#: multi-identity operator surface is out of scope until there is more than
#: one tool or one caller to distinguish (P1+).
_AGENT_ID = "agent_investigator_0"


class _PreGeneratedIdGenerator:
    """Yields exactly one, already-chosen run id.

    `Investigator.investigate()` mints its own run id internally
    (`self._id_generator.new_run_id()`), which the runner needs to know
    *before* that call so it can name the audit-journal path. Rather than
    have the runner guess a run id and hope, or have `Investigator` accept a
    pre-chosen id (widening its contract for a CLI-only concern), the runner
    generates the real id itself via `Uuid4IdGenerator` and hands the
    `Investigator` a trivial generator that just returns it - `IdGenerator`
    is a port precisely so a caller can supply the implementation its own
    context needs (ADR-0004).
    """

    def __init__(self, run_id: RunId) -> None:
        self._run_id = run_id

    def new_run_id(self) -> RunId:
        return self._run_id


@dataclass(frozen=True)
class RunArtifacts:
    """Where one investigation's durable records were written, plus the
    result itself. Not a domain type - purely a CLI-layer return value."""

    run: InvestigationRun
    audit_path: Path
    result_path: Path


def _evidence_ids_in(history: tuple[ConversationMessage, ...]) -> list[EvidenceId]:
    """Every evidence id the conversation actually contains, in collection
    order - read back from real tool results, never assumed. The same
    pattern `tests/integration/test_inc001_agent_investigation.py` uses,
    because the dry-run demo must earn its citations exactly as any other
    scripted investigation does (Engineering Contract, P0.6 9)."""
    ids: list[EvidenceId] = []
    for message in history:
        for block in message.content:
            if isinstance(block, ToolResultBlock) and "evidence_id:" in block.content:
                line = next(
                    ln for ln in block.content.splitlines() if ln.startswith("evidence_id:")
                )
                ids.append(EvidenceId(line.split(":", 1)[1].strip()))
    return ids


def _dry_run_final_turn(history: tuple[ConversationMessage, ...]) -> ModelTurn:
    """The scripted dry-run's conclusion, built from whichever evidence ids
    the earlier scripted tool calls actually produced - never a hardcoded
    `ev_001` (Engineering Contract, P0.6 9)."""
    ids = _evidence_ids_in(history)
    return ModelTurn(
        analysis=Analysis(
            outcome=AnalysisOutcome.COMPLETED,
            summary=(
                "Revenue under-reported on 2026-09-07 because a new upstream order "
                "status is excluded by the revenue_daily filter."
            ),
            root_cause=(
                "analytics.revenue_daily aggregates only status = 'COMPLETED'. On "
                "2026-09-07 most orders were classified PENDING_CAPTURE instead, "
                "while total order volume was unaffected, so the aggregate silently "
                "excludes real revenue."
            ),
            claims=tuple(
                Claim(statement=statement, citations=(evidence_id,))
                for statement, evidence_id in zip(
                    (
                        "Order volume on 2026-09-07 matched prior days, not data loss.",
                        "PENDING_CAPTURE appeared only on 2026-09-07, displacing COMPLETED.",
                        "Reported revenue dropped sharply on the incident date.",
                    ),
                    ids,
                    strict=True,
                )
            ),
            confidence=Confidence.HIGH,
        )
    )


def _dry_run_script() -> list[ScriptEntry]:
    """A deterministic, offline script that drives the *real* INC-001 stack -
    real DuckDB, real `ToolExecutor`/AuthZ, real citation validation - with
    only the model faked. Mirrors the queries a Phase 1 human investigator
    would run first; see `tests/integration/test_inc001_agent_investigation.py`
    for the same pattern under test.

    Deliberately INC-001-shaped rather than incident-generic: Phase 0 has
    exactly one incident fixture, and building a general-purpose demo script
    for incidents that do not exist yet is exactly the kind of speculative
    generality the Engineering Contract's dependency/scope policy warns
    against. A second incident (P1) would need its own script.
    """
    return [
        ModelTurn(
            tool_calls=(
                ToolCallBlock(
                    call_id="toolu_1",
                    tool_name="query_warehouse",
                    arguments={
                        "sql": (
                            "SELECT CAST(order_ts AS DATE) AS order_date, count(*) AS n, "
                            "sum(amount_usd) AS gross FROM raw.orders GROUP BY 1 ORDER BY 1"
                        ),
                        "reason": "Confirm whether upstream order volume dropped that day.",
                    },
                ),
            )
        ),
        ModelTurn(
            tool_calls=(
                ToolCallBlock(
                    call_id="toolu_2",
                    tool_name="query_warehouse",
                    arguments={
                        "sql": (
                            "SELECT CAST(order_ts AS DATE) AS order_date, status, "
                            "count(*) AS n FROM raw.orders GROUP BY 1, 2 ORDER BY 1, 2"
                        ),
                        "reason": "Check whether the status mix changed around that date.",
                    },
                ),
            )
        ),
        ModelTurn(
            tool_calls=(
                ToolCallBlock(
                    call_id="toolu_3",
                    tool_name="query_warehouse",
                    arguments={
                        "sql": "SELECT * FROM analytics.revenue_daily ORDER BY order_date",
                        "reason": "Read the reported daily revenue trend directly.",
                    },
                ),
            )
        ),
        _dry_run_final_turn,
    ]


def build_model_client(*, dry_run: bool, settings: Settings) -> ModelClient:
    """The one place `--dry-run` and live mode diverge. Everything else - the
    registry, the identity, the budget, the audit sink - is identical."""
    if dry_run:
        return FakeModelClient(_dry_run_script())
    return AnthropicModelClient.from_settings(settings)


def build_registry(*, warehouse_path: Path = DEFAULT_WAREHOUSE_PATH) -> ToolRegistry:
    """The real tool registry: `query_warehouse` plus every registered
    artifact-evidence tool (P1.1 adds `query_airflow_runs`).

    Building the warehouse file on first use (rather than requiring a
    separate manual step) is what lets a fresh clone reach a working
    `--dry-run` in one command - `build_warehouse_db` is the same,
    already-reviewed function `scripts/seed_warehouse.py` calls; this is not
    a new way of writing to the warehouse, only a convenience call to the
    existing one (`causiq.evidence_substrate` remains the only module that
    opens the file read-write). `AirflowDagRunsTool` needs no equivalent
    bootstrap step - its fixture is a static, already-committed file (P1
    architecture plan §9), so it is simply constructed and registered.
    """
    if not warehouse_path.exists():
        build_warehouse_db(warehouse_path)
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_path))
    registry.register(AirflowDagRunsTool())
    return registry


def _load_incident(incident_id: str) -> Incident:
    """Raises `FileNotFoundError` for an unknown incident id - allowed to
    propagate to the CLI layer, which turns it into a clean usage error
    rather than a traceback (this is an operator mistake, not a bug)."""
    return load_incident_by_id(incident_id)


def run_investigation(
    incident_id: str,
    *,
    dry_run: bool,
    settings: Settings | None = None,
    id_generator: IdGenerator | None = None,
    clock: Clock | None = None,
    warehouse_path: Path | None = None,
) -> RunArtifacts:
    """Investigate `incident_id` and persist the result.

    Raises `FileNotFoundError` for an unknown incident and
    `causiq.errors.ConfigurationError` for a live invocation with no
    `ANTHROPIC_API_KEY` - both before any tool registry or warehouse file is
    touched, so a misconfigured invocation fails before doing any work.

    `id_generator`/`clock`/`warehouse_path` are test seams only (so a test
    can inject `FixedIdGenerator`/`FrozenClock`/an isolated temp warehouse -
    e.g. for the real-agent determinism proof in
    `tests/integration/test_real_agent_determinism.py`); the CLI itself never
    passes them, and production use always gets `Uuid4IdGenerator`,
    `SystemClock`, and the shared `DEFAULT_WAREHOUSE_PATH`.
    """
    settings = settings if settings is not None else load_settings()
    incident = _load_incident(incident_id)
    model = build_model_client(dry_run=dry_run, settings=settings)
    registry = (
        build_registry(warehouse_path=warehouse_path)
        if warehouse_path is not None
        else build_registry()
    )
    identity = AgentIdentity(
        agent_id=_AGENT_ID,
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset({Permission.WAREHOUSE_READ}),
    )

    run_id_source = id_generator if id_generator is not None else Uuid4IdGenerator()
    run_id = run_id_source.new_run_id()
    run_clock = clock if clock is not None else SystemClock()

    investigator = Investigator(
        model=model,
        registry=registry,
        identity=identity,
        id_generator=_PreGeneratedIdGenerator(run_id),
        clock=run_clock,
    )

    settings.audit_dir.mkdir(parents=True, exist_ok=True)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    audit_path = settings.audit_dir / f"{run_id}.jsonl"
    result_path = settings.runs_dir / f"{run_id}.json"

    with JsonlAuditSink(audit_path) as sink:
        run = investigator.investigate(incident, budget=settings.default_budget(), audit_sink=sink)

    # Written unconditionally, regardless of terminal state - a
    # BUDGET_EXCEEDED or FAILED run's partial evidence is exactly as real as
    # a COMPLETED one's, and AC-9 requires it survive the process (see the
    # module docstring's persistence boundary).
    result_path.write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")

    return RunArtifacts(run=run, audit_path=audit_path, result_path=result_path)


__all__: Sequence[str] = (
    "RunArtifacts",
    "build_model_client",
    "build_registry",
    "run_investigation",
)
