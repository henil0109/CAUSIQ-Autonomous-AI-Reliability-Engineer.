"""INC-001, end to end, through the real bounded agent (P0.6 9).

This is the test the whole of P0.6 exists to make possible: a real
`Investigator`, driving a real `ToolExecutor` / `ToolRegistry` / `AuthZ` /
`EvidenceLedger` stack and a real `query_warehouse` tool against a real
DuckDB file, with only the *model* replaced by a deterministic
`FakeModelClient` script.

The script asks for exactly the evidence a Phase-1 human investigator would:
source volume, status distribution, and the reported revenue trend - then
concludes by citing the evidence ids the tool calls actually produced. No
evidence id is ever hardcoded: the final turn is a callable that reads them
out of the conversation history the same way a real model would have to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from causiq.agents import Investigator
from causiq.audit import InMemoryAuditSink
from causiq.authz import AgentIdentity, Permission
from causiq.clock import FrozenClock
from causiq.domain import (
    AgentRole,
    Analysis,
    AnalysisOutcome,
    AuditEventType,
    Budget,
    Claim,
    Confidence,
    RunState,
)
from causiq.evidence_substrate import load_incident_by_id
from causiq.ids import EvidenceId, FixedIdGenerator
from causiq.llm.fake_client import FakeModelClient
from causiq.llm.ports import ConversationMessage, ModelTurn, ToolCallBlock, ToolResultBlock
from causiq.tools import ToolRegistry
from causiq.tools.warehouse import QueryWarehouseTool

pytestmark = pytest.mark.integration


def _evidence_ids_in(history: tuple[ConversationMessage, ...]) -> list[EvidenceId]:
    """Every evidence id the conversation actually contains, in the order the
    tool calls produced them - never assumed, always read back."""
    ids: list[EvidenceId] = []
    for message in history:
        for block in message.content:
            if isinstance(block, ToolResultBlock) and "evidence_id:" in block.content:
                line = next(
                    ln for ln in block.content.splitlines() if ln.startswith("evidence_id:")
                )
                ids.append(EvidenceId(line.split(":", 1)[1].strip()))
    return ids


def _final_analysis(history: tuple[ConversationMessage, ...]) -> ModelTurn:
    """The scripted "model's" conclusion - built from whatever evidence ids
    the earlier scripted tool calls actually produced this run."""
    ids = _evidence_ids_in(history)
    assert len(ids) == 3, "expected exactly three prior tool results in this script"
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
            claims=(
                Claim(
                    statement="Order volume on 2026-09-07 matched prior days, not data loss.",
                    citations=(ids[0],),
                ),
                Claim(
                    statement="PENDING_CAPTURE appeared only on 2026-09-07, displacing COMPLETED.",
                    citations=(ids[1],),
                ),
                Claim(
                    statement="Reported revenue dropped sharply on the incident date.",
                    citations=(ids[2],),
                ),
            ),
            confidence=Confidence.HIGH,
        )
    )


@pytest.fixture
def script() -> FakeModelClient:
    return FakeModelClient(
        [
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
            _final_analysis,
        ]
    )


@pytest.fixture
def agent(warehouse_db_path: Path, script: FakeModelClient) -> Investigator:
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    identity = AgentIdentity(
        agent_id="agent_investigator_0",
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset({Permission.WAREHOUSE_READ}),
    )
    return Investigator(
        model=script,
        registry=registry,
        identity=identity,
        id_generator=FixedIdGenerator(),
        clock=FrozenClock(),
        system_prompt="test investigator - see this file",
    )


def test_inc001_completes_end_to_end_through_the_real_stack(agent: Investigator) -> None:
    incident = load_incident_by_id("INC-001")
    sink = InMemoryAuditSink()
    budget = Budget(max_turns=6, max_tool_calls=6, max_total_tokens=1_000_000, deadline_seconds=120)

    run = agent.investigate(incident, budget=budget, audit_sink=sink)

    # 1 & 10: the agent received the incident and completed.
    assert run.incident_id == incident.incident_id
    assert run.state is RunState.COMPLETED

    # 4, 5, 6: real evidence was recorded for each of the three tool calls,
    # via the real Registry -> Executor -> AuthZ -> query_warehouse -> DuckDB
    # path (there is no other code path by which this ledger could be
    # populated - causiq.tools.warehouse.QueryWarehouseTool.run has no
    # ledger/journal parameter at all).
    assert len(run.evidence) == 3
    for evidence in run.evidence:
        assert evidence.verify()
        assert evidence.request.tool_name == "query_warehouse"
        assert evidence.agent_id == "agent_investigator_0"

    # 7: the model asked for more than one piece of evidence before concluding.
    assert len(run.evidence) > 1

    # 8 & 9: the final analysis is structured and its citations resolve
    # against evidence actually created *this run* - not hardcoded ids.
    assert run.analysis is not None
    assert run.analysis.outcome is AnalysisOutcome.COMPLETED
    cited_ids = run.analysis.cited_evidence()
    real_ids = run.evidence_ids()
    assert cited_ids, "the analysis must cite something"
    assert cited_ids <= real_ids, "every citation must resolve to evidence from this run"
    assert cited_ids == real_ids, "this script's final turn cites every piece of evidence collected"

    # The audit trail tells the same story independently of the domain model.
    events = sink.events()
    assert events[0] is AuditEventType.RUN_STARTED
    assert events[-1] is AuditEventType.RUN_ENDED
    assert events.count(AuditEventType.TOOL_AUTHORIZED) == 3
    assert events.count(AuditEventType.EVIDENCE_RECORDED) == 3
    assert AuditEventType.ANALYSIS_VALIDATED in events


def test_evidence_ids_are_ledger_generated_not_assumed(agent: Investigator) -> None:
    """Direct check on the property the task calls out explicitly: the ids
    the final analysis cites are whatever the ledger actually minted, read
    back from tool results - the test never asserts a literal `ev_001`."""
    incident = load_incident_by_id("INC-001")
    run = agent.investigate(
        incident,
        budget=Budget(
            max_turns=6, max_tool_calls=6, max_total_tokens=1_000_000, deadline_seconds=120
        ),
        audit_sink=InMemoryAuditSink(),
    )
    assert run.analysis is not None
    ledger_ids = {str(e.evidence_id) for e in run.evidence}
    cited_ids = {str(c) for c in run.analysis.cited_evidence()}
    assert cited_ids == ledger_ids


# --------------------------------------------------------------------------- #
# The critical negative case, against the real stack (P0.6 8)
# --------------------------------------------------------------------------- #
def test_fabricated_citation_against_the_real_stack_is_rejected(
    warehouse_db_path: Path,
) -> None:
    """The one negative test the task calls a critical P0 acceptance
    criterion, run against the real tool and a real database - not just a
    test double."""
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    identity = AgentIdentity(
        agent_id="agent_investigator_0",
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset({Permission.WAREHOUSE_READ}),
    )
    model = FakeModelClient(
        [
            ModelTurn(
                tool_calls=(
                    ToolCallBlock(
                        call_id="t1",
                        tool_name="query_warehouse",
                        arguments={
                            "sql": "SELECT count(*) FROM raw.orders",
                            "reason": "sanity check",
                        },
                    ),
                )
            ),
            ModelTurn(
                analysis=Analysis(
                    outcome=AnalysisOutcome.COMPLETED,
                    summary="s",
                    root_cause="rc",
                    confidence=Confidence.HIGH,
                    claims=(Claim(statement="x", citations=(EvidenceId("ev_does_not_exist"),)),),
                )
            ),
        ]
    )
    agent = Investigator(
        model=model,
        registry=registry,
        identity=identity,
        id_generator=FixedIdGenerator(),
        clock=FrozenClock(),
        system_prompt="test",
    )
    sink = InMemoryAuditSink()
    run = agent.investigate(
        load_incident_by_id("INC-001"),
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60),
        audit_sink=sink,
    )

    assert run.state is RunState.FAILED
    assert run.failure_reason is not None and "ev_does_not_exist" in run.failure_reason
    assert AuditEventType.ANALYSIS_REJECTED in sink.events()
    assert AuditEventType.ANALYSIS_VALIDATED not in sink.events()
    # the real evidence collected is untouched by the rejection
    assert len(run.evidence) == 1
    assert run.evidence[0].verify()
