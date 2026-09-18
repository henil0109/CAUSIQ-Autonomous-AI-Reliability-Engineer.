"""Determinism, proven against the *real* agent loop (AC-10, invariant I6, P0.7).

`tests/integration/test_determinism.py` proves byte-identical audit journals
across two frozen-clock runs, but only for a hand-simulated slice of P0.1-P0.3
components - its own docstring said "not the real agent loop - that is
P0.7," written before `Investigator` existed. This file closes that gap: the
same property, proven against the real `Investigator`, the real
`ToolRegistry`/`ToolExecutor`/AuthZ path, and a real DuckDB warehouse, with
only the model scripted via `FakeModelClient`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from causiq.agents import Investigator
from causiq.audit import JsonlAuditSink
from causiq.authz import AgentIdentity, Permission
from causiq.clock import FrozenClock
from causiq.domain import AgentRole, Analysis, AnalysisOutcome, Budget, Confidence, RunState
from causiq.evidence_substrate import load_incident_by_id
from causiq.ids import FixedIdGenerator
from causiq.llm.fake_client import FakeModelClient, ScriptEntry
from causiq.llm.ports import ModelTurn, ToolCallBlock
from causiq.tools import ToolRegistry
from causiq.tools.warehouse import QueryWarehouseTool

pytestmark = pytest.mark.integration


def _final_turn(history: object) -> ModelTurn:
    del history  # deliberately unused - this test is about determinism, not citations
    return ModelTurn(
        analysis=Analysis(
            outcome=AnalysisOutcome.INCONCLUSIVE,
            summary="s",
            limitations="deliberately inconclusive - this test proves determinism, not RCA",
            confidence=Confidence.LOW,
        )
    )


def _script() -> list[ScriptEntry]:
    """A fresh script each call - `FakeModelClient` consumes its script in
    place, so the two runs under comparison must each build their own from
    identical literals, never share one instance."""
    return [
        ModelTurn(
            tool_calls=(
                ToolCallBlock(
                    call_id="toolu_1",
                    tool_name="query_warehouse",
                    arguments={"sql": "SELECT count(*) FROM raw.orders", "reason": "sanity check"},
                ),
            )
        ),
        _final_turn,
    ]


def _run_once(warehouse_path: Path, journal_path: Path) -> bytes:
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_path))
    identity = AgentIdentity(
        agent_id="agent_investigator_0",
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset({Permission.WAREHOUSE_READ}),
    )
    investigator = Investigator(
        model=FakeModelClient(_script()),
        registry=registry,
        identity=identity,
        id_generator=FixedIdGenerator(),
        clock=FrozenClock(),
        system_prompt="deterministic test - see tests/integration/test_real_agent_determinism.py",
    )
    budget = Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60)

    with JsonlAuditSink(journal_path) as sink:
        run = investigator.investigate(
            load_incident_by_id("INC-001"), budget=budget, audit_sink=sink
        )

    assert run.state is RunState.INCONCLUSIVE
    return journal_path.read_bytes()


def test_two_real_agent_runs_produce_byte_identical_journals(
    tmp_path: Path, warehouse_db_path: Path
) -> None:
    """AC-10, against the actual shipped agent loop."""
    first = _run_once(warehouse_db_path, tmp_path / "run_a.jsonl")
    second = _run_once(warehouse_db_path, tmp_path / "run_b.jsonl")

    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


def test_real_agent_journal_reconstructs_the_run_without_logs(
    tmp_path: Path, warehouse_db_path: Path
) -> None:
    """Invariant I7 against the real loop: the journal alone tells the
    story, matching `test_determinism.py`'s equivalent for the old slice."""
    content = _run_once(warehouse_db_path, tmp_path / "run.jsonl").decode("utf-8")
    events = [line for line in content.splitlines() if line.strip()]

    assert "run.started" in events[0]
    assert "run.ended" in events[-1]
    assert any("tool.authorized" in line for line in events)
    assert any("evidence.recorded" in line for line in events)
    assert any("analysis.validated" in line for line in events)
