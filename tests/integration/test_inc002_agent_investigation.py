"""INC-002, end to end, through the real bounded agent (P1.3).

This is the test P1.3 exists to make possible: a real `Investigator`, driving
a real `ToolExecutor` / `ToolRegistry` / `AuthZ` / `EvidenceLedger` stack and
FOUR real tools - `query_dq_check_results`, `query_warehouse`,
`query_dbt_run_results`, `query_airflow_runs` - against real fixtures, with
only the *model* replaced by a deterministic `FakeModelClient` script.

The script asks for exactly the evidence a human investigator would, working
backwards from the observed symptom: the DQ check that caught the problem,
the warehouse table showing the missing refresh, the dbt run that errored,
and the Airflow task that failed upstream of it - then concludes by citing
the evidence ids the tool calls actually produced. No evidence id is ever
hardcoded: the final turn is a callable that reads them out of the
conversation history the same way a real model would have to. Mirrors
`test_inc001_agent_investigation.py`'s discipline exactly, extended from one
source to four.
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
    EvidenceSource,
    RunState,
)
from causiq.evidence_substrate import load_incident_by_id
from causiq.ids import EvidenceId, FixedIdGenerator
from causiq.llm.fake_client import FakeModelClient
from causiq.llm.ports import ConversationMessage, ModelTurn, ToolCallBlock, ToolResultBlock
from causiq.tools import ToolRegistry
from causiq.tools.airflow import AirflowDagRunsTool
from causiq.tools.dbt import DbtRunResultsTool
from causiq.tools.dq import DqCheckResultsTool
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
    assert len(ids) == 4, "expected exactly four prior tool results in this script"
    return ModelTurn(
        analysis=Analysis(
            outcome=AnalysisOutcome.COMPLETED,
            summary=(
                "analytics.revenue_daily was not refreshed for 2026-09-05 because the "
                "upstream Airflow load task failed, which caused the dependent dbt "
                "model run to error - caught the next check cycle by the "
                "revenue_daily_freshness data-quality check."
            ),
            root_cause=(
                "The daily_revenue_pipeline DAG's load_orders task failed on "
                "2026-09-05, running far past its normal duration before failing. The "
                "dependent dbt model daily_revenue_pipeline, scheduled independently, "
                "errored quickly the same morning. As a result no successful refresh "
                "was recorded for that date, and the revenue_daily_freshness check "
                "failed once its measured staleness exceeded the threshold."
            ),
            claims=(
                Claim(
                    statement=(
                        "The revenue_daily_freshness check failed on 2026-09-05 with "
                        "measured staleness far exceeding its threshold."
                    ),
                    citations=(ids[0],),
                ),
                Claim(
                    statement=(
                        "analytics.model_refresh_log has no successful refresh recorded "
                        "for daily_revenue_pipeline on 2026-09-05."
                    ),
                    citations=(ids[1],),
                ),
                Claim(
                    statement=(
                        "The dbt daily_revenue_pipeline model run on 2026-09-05 errored "
                        "after only 2 seconds, far short of its normal runtime."
                    ),
                    citations=(ids[2],),
                ),
                Claim(
                    statement=(
                        "The Airflow load_orders task for daily_revenue_pipeline failed "
                        "on 2026-09-05 after running roughly 4x longer than normal."
                    ),
                    citations=(ids[3],),
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
                        tool_name="query_dq_check_results",
                        arguments={
                            "check_name": "revenue_daily_freshness",
                            "reason": "The incident was detected via this check - see its history.",
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
                                "SELECT * FROM analytics.model_refresh_log "
                                "WHERE model_name = 'daily_revenue_pipeline' "
                                "ORDER BY order_date"
                            ),
                            "reason": "Confirm whether the table actually missed a refresh.",
                        },
                    ),
                )
            ),
            ModelTurn(
                tool_calls=(
                    ToolCallBlock(
                        call_id="toolu_3",
                        tool_name="query_dbt_run_results",
                        arguments={
                            "unique_id": "model.revenue_analytics.daily_revenue_pipeline",
                            "reason": "Check whether the transformation itself failed.",
                        },
                    ),
                )
            ),
            ModelTurn(
                tool_calls=(
                    ToolCallBlock(
                        call_id="toolu_4",
                        tool_name="query_airflow_runs",
                        arguments={
                            "dag_id": "daily_revenue_pipeline",
                            "reason": "Check whether an upstream task failed that day.",
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
    registry.register(AirflowDagRunsTool())
    registry.register(DbtRunResultsTool())
    registry.register(DqCheckResultsTool())
    identity = AgentIdentity(
        agent_id="agent_investigator_0",
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset({Permission.WAREHOUSE_READ, Permission.ARTIFACTS_READ}),
    )
    return Investigator(
        model=script,
        registry=registry,
        identity=identity,
        id_generator=FixedIdGenerator(),
        clock=FrozenClock(),
        system_prompt="test investigator - see this file",
    )


def test_inc002_completes_end_to_end_through_the_real_stack(agent: Investigator) -> None:
    incident = load_incident_by_id("INC-002")
    sink = InMemoryAuditSink()
    budget = Budget(max_turns=8, max_tool_calls=8, max_total_tokens=1_000_000, deadline_seconds=120)

    run = agent.investigate(incident, budget=budget, audit_sink=sink)

    # The agent received the incident and completed.
    assert run.incident_id == incident.incident_id
    assert run.state is RunState.COMPLETED

    # Real evidence was recorded for each of the four tool calls, via the
    # real Registry -> Executor -> AuthZ -> tool -> ledger path.
    assert len(run.evidence) == 4
    for evidence in run.evidence:
        assert evidence.verify()
        assert evidence.agent_id == "agent_investigator_0"

    # Evidence genuinely spans all four sources - not four calls to one tool.
    sources = {evidence.source for evidence in run.evidence}
    assert sources == {
        EvidenceSource.DATA_QUALITY,
        EvidenceSource.WAREHOUSE,
        EvidenceSource.DBT,
        EvidenceSource.AIRFLOW,
    }

    # The final analysis is structured and its citations resolve against
    # evidence actually created *this run* - not hardcoded ids.
    assert run.analysis is not None
    assert run.analysis.outcome is AnalysisOutcome.COMPLETED
    cited_ids = run.analysis.cited_evidence()
    real_ids = run.evidence_ids()
    assert cited_ids, "the analysis must cite something"
    assert cited_ids <= real_ids, "every citation must resolve to evidence from this run"
    assert cited_ids == real_ids, "this script's final turn cites every piece of evidence collected"

    # Every source is actually cited, not merely collected.
    cited_sources = {
        evidence.source for evidence in run.evidence if evidence.evidence_id in cited_ids
    }
    assert cited_sources == sources

    # The audit trail tells the same story independently of the domain model.
    events = sink.events()
    assert events[0] is AuditEventType.RUN_STARTED
    assert events[-1] is AuditEventType.RUN_ENDED
    assert events.count(AuditEventType.TOOL_AUTHORIZED) == 4
    assert events.count(AuditEventType.EVIDENCE_RECORDED) == 4
    assert AuditEventType.ANALYSIS_VALIDATED in events
    assert AuditEventType.TOOL_DENIED not in events


def test_evidence_ids_are_ledger_generated_not_assumed(agent: Investigator) -> None:
    """Direct check on the property the task calls out explicitly: the ids
    the final analysis cites are whatever the ledger actually minted, read
    back from tool results - the test never asserts a literal `ev_001`."""
    incident = load_incident_by_id("INC-002")
    run = agent.investigate(
        incident,
        budget=Budget(
            max_turns=8, max_tool_calls=8, max_total_tokens=1_000_000, deadline_seconds=120
        ),
        audit_sink=InMemoryAuditSink(),
    )
    assert run.analysis is not None
    ledger_ids = {str(e.evidence_id) for e in run.evidence}
    cited_ids = {str(c) for c in run.analysis.cited_evidence()}
    assert cited_ids == ledger_ids


# --------------------------------------------------------------------------- #
# The critical negative case, against the real four-tool stack (P0.6 8)
# --------------------------------------------------------------------------- #
def test_fabricated_citation_against_the_real_stack_is_rejected(warehouse_db_path: Path) -> None:
    """The one negative test the task calls a critical P0 acceptance
    criterion, repeated here against the real INC-002 four-tool stack."""
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    registry.register(AirflowDagRunsTool())
    registry.register(DbtRunResultsTool())
    registry.register(DqCheckResultsTool())
    identity = AgentIdentity(
        agent_id="agent_investigator_0",
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset({Permission.WAREHOUSE_READ, Permission.ARTIFACTS_READ}),
    )
    model = FakeModelClient(
        [
            ModelTurn(
                tool_calls=(
                    ToolCallBlock(
                        call_id="t1",
                        tool_name="query_dq_check_results",
                        arguments={
                            "check_name": "revenue_daily_freshness",
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
        load_incident_by_id("INC-002"),
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
    assert run.evidence[0].source == EvidenceSource.DATA_QUALITY
