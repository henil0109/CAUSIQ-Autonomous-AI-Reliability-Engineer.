"""`AirflowDagRunsTool`, through the real capability layer (P1.1).

Mirrors `tests/integration/test_inc001_agent_investigation.py`'s discipline
for the warehouse tool: real `ToolRegistry`, real `ToolExecutor`, real
`authz.authorize`, real `EvidenceLedger`, real `AuditJournal` - nothing about
the security/evidence boundary is mocked. Only this file proves that the
Airflow tool's output actually becomes `Evidence` (and only through the
executor); `test_airflow_tool.py` proves the tool's own behavior in isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from causiq.audit import AuditJournal, InMemoryAuditSink
from causiq.authz import AgentIdentity, Permission
from causiq.clock import FrozenClock
from causiq.domain import AgentRole, AuditEventType, EvidenceSource
from causiq.evidence import EvidenceLedger
from causiq.tools import ToolExecutor, ToolRegistry, ToolRequest, ToolResultStatus, tool_schema
from causiq.tools.airflow import AirflowDagRunsTool
from causiq.tools.warehouse import QueryWarehouseTool
from tests.conftest import RUN_ID, T0

pytestmark = pytest.mark.integration


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(AirflowDagRunsTool())
    return registry


def _executor(
    *,
    registry: ToolRegistry,
    ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> ToolExecutor:
    clock = FrozenClock(T0)
    return ToolExecutor(
        registry=registry,
        identity=identity,
        ledger=ledger,
        journal=AuditJournal(RUN_ID, clock, sink),
        clock=clock,
    )


@pytest.fixture
def read_identity() -> AgentIdentity:
    return AgentIdentity(
        agent_id="agent_investigator_0",
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset({Permission.ARTIFACTS_READ}),
    )


@pytest.fixture
def powerless_identity() -> AgentIdentity:
    return AgentIdentity(
        agent_id="agent_powerless",
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset(),
    )


# --------------------------------------------------------------------------- #
# A. Authorized call -> real Evidence
# --------------------------------------------------------------------------- #
def test_authorized_call_produces_verified_evidence(read_identity: AgentIdentity) -> None:
    ledger = EvidenceLedger(RUN_ID)
    sink = InMemoryAuditSink()
    executor = _executor(registry=_registry(), ledger=ledger, sink=sink, identity=read_identity)

    request = ToolRequest(
        tool_use_id="t1",
        tool_name="query_airflow_runs",
        arguments={"dag_id": "daily_revenue_pipeline", "reason": "check pipeline history"},
    )
    result = executor.execute(request)

    assert result.status is ToolResultStatus.OK
    assert result.is_error is False
    assert result.evidence_id is not None

    assert len(ledger) == 1
    evidence = ledger.snapshot()[0]
    assert evidence.verify()
    assert evidence.source == EvidenceSource.AIRFLOW
    assert evidence.agent_id == "agent_investigator_0"
    assert evidence.request.tool_name == "query_airflow_runs"
    assert evidence.request.arguments == {"dag_id": "daily_revenue_pipeline"}
    assert evidence.request.reason == "check pipeline history"

    parsed_content = json.loads(evidence.content)
    assert parsed_content["dag_id"] == "daily_revenue_pipeline"

    events = sink.events()
    assert AuditEventType.TOOL_AUTHORIZED in events
    assert AuditEventType.TOOL_EXECUTED in events
    assert AuditEventType.EVIDENCE_RECORDED in events
    assert AuditEventType.TOOL_DENIED not in events
    assert AuditEventType.TOOL_FAILED not in events


def test_evidence_id_is_returned_to_the_model_in_the_tool_result(
    read_identity: AgentIdentity,
) -> None:
    ledger = EvidenceLedger(RUN_ID)
    executor = _executor(
        registry=_registry(), ledger=ledger, sink=InMemoryAuditSink(), identity=read_identity
    )
    request = ToolRequest(
        tool_use_id="t1",
        tool_name="query_airflow_runs",
        arguments={"dag_id": "customer_ltv_pipeline", "reason": "r"},
    )
    result = executor.execute(request)
    assert str(result.evidence_id) in result.content
    assert "evidence_id:" in result.content


# --------------------------------------------------------------------------- #
# B. Unauthorized identity -> denied, no evidence
# --------------------------------------------------------------------------- #
def test_unauthorized_identity_is_denied(powerless_identity: AgentIdentity) -> None:
    ledger = EvidenceLedger(RUN_ID)
    sink = InMemoryAuditSink()
    executor = _executor(
        registry=_registry(), ledger=ledger, sink=sink, identity=powerless_identity
    )

    request = ToolRequest(
        tool_use_id="t1",
        tool_name="query_airflow_runs",
        arguments={"dag_id": "daily_revenue_pipeline", "reason": "r"},
    )
    result = executor.execute(request)

    assert result.status is ToolResultStatus.DENIED
    assert result.is_error is True
    assert result.evidence_id is None
    assert len(ledger) == 0

    events = sink.events()
    assert AuditEventType.TOOL_DENIED in events
    assert AuditEventType.EVIDENCE_RECORDED not in events


# --------------------------------------------------------------------------- #
# C. Unknown DAG -> ordinary failure, no evidence
# --------------------------------------------------------------------------- #
def test_unknown_dag_fails_without_creating_evidence(read_identity: AgentIdentity) -> None:
    ledger = EvidenceLedger(RUN_ID)
    sink = InMemoryAuditSink()
    executor = _executor(registry=_registry(), ledger=ledger, sink=sink, identity=read_identity)

    request = ToolRequest(
        tool_use_id="t1",
        tool_name="query_airflow_runs",
        arguments={"dag_id": "not_a_real_dag", "reason": "r"},
    )
    result = executor.execute(request)

    assert result.status is ToolResultStatus.FAILED
    assert result.is_error is True
    assert result.evidence_id is None
    assert len(ledger) == 0

    events = sink.events()
    assert AuditEventType.TOOL_FAILED in events
    assert AuditEventType.EVIDENCE_RECORDED not in events


def test_malformed_dag_id_is_rejected_as_invalid_input_not_a_failure(
    read_identity: AgentIdentity,
) -> None:
    """A path-traversal-shaped argument never reaches `AirflowDagRunsTool.run` -
    the executor's own schema validation (Gate 3) rejects it first."""
    ledger = EvidenceLedger(RUN_ID)
    sink = InMemoryAuditSink()
    executor = _executor(registry=_registry(), ledger=ledger, sink=sink, identity=read_identity)

    request = ToolRequest(
        tool_use_id="t1",
        tool_name="query_airflow_runs",
        arguments={"dag_id": "../../etc/passwd", "reason": "r"},
    )
    result = executor.execute(request)

    assert result.status is ToolResultStatus.INVALID_INPUT
    assert result.evidence_id is None
    assert len(ledger) == 0
    assert AuditEventType.TOOL_INPUT_REJECTED in sink.events()


# --------------------------------------------------------------------------- #
# Warehouse coexistence - both tools registered together (the real runner shape)
# --------------------------------------------------------------------------- #
def test_airflow_and_warehouse_tools_coexist_in_one_registry(warehouse_db_path: Path) -> None:
    """Sanity check matching `runner.build_registry()`'s actual shape: both
    tools registered together, each still attributing evidence to its own
    `EvidenceSource`, with a `WAREHOUSE_READ`+`ARTIFACTS_READ` identity able
    to use both."""
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    registry.register(AirflowDagRunsTool())
    assert registry.names() == ("query_airflow_runs", "query_warehouse")

    both_identity = AgentIdentity(
        agent_id="agent_investigator_0",
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset({Permission.WAREHOUSE_READ, Permission.ARTIFACTS_READ}),
    )
    ledger = EvidenceLedger(RUN_ID)
    executor = _executor(
        registry=registry, ledger=ledger, sink=InMemoryAuditSink(), identity=both_identity
    )

    airflow_result = executor.execute(
        ToolRequest(
            tool_use_id="t1",
            tool_name="query_airflow_runs",
            arguments={"dag_id": "daily_revenue_pipeline", "reason": "r"},
        )
    )
    warehouse_result = executor.execute(
        ToolRequest(
            tool_use_id="t2",
            tool_name="query_warehouse",
            arguments={"sql": "SELECT count(*) FROM raw.orders", "reason": "r"},
        )
    )

    assert airflow_result.status is ToolResultStatus.OK
    assert warehouse_result.status is ToolResultStatus.OK
    assert len(ledger) == 2
    sources = {evidence.source for evidence in ledger.snapshot()}
    assert sources == {EvidenceSource.AIRFLOW, EvidenceSource.WAREHOUSE}


# --------------------------------------------------------------------------- #
# Schema determinism after adding a second tool (Engineering Contract 6.3)
# --------------------------------------------------------------------------- #
def test_registry_schema_digest_is_stable_across_calls_with_both_tools(
    warehouse_db_path: Path,
) -> None:
    """Adding Airflow must not make the emitted tool list unstable - the same
    `schema_digest` property already proven for a single tool must still hold
    with two, since the cached prompt prefix depends on byte-for-byte
    stability (Engineering Contract 6.3)."""
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    registry.register(AirflowDagRunsTool())

    first_digest = registry.schema_digest()
    second_digest = registry.schema_digest()
    assert first_digest == second_digest

    first_schemas = registry.schemas()
    second_schemas = registry.schemas()
    assert first_schemas == second_schemas


def test_adding_airflow_does_not_change_the_warehouse_tools_own_schema(
    warehouse_db_path: Path,
) -> None:
    """Registering a second tool must not perturb the first tool's own
    emitted schema - each tool's schema is a pure function of that tool
    alone, never of what else happens to share the registry."""
    solo_schema = tool_schema(QueryWarehouseTool(warehouse_db_path))

    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    registry.register(AirflowDagRunsTool())
    warehouse_schema_in_registry = next(
        schema for schema in registry.schemas() if schema["name"] == "query_warehouse"
    )

    assert warehouse_schema_in_registry == solo_schema


def test_registry_emits_tools_in_deterministic_name_sorted_order(
    warehouse_db_path: Path,
) -> None:
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    registry.register(AirflowDagRunsTool())
    names = [schema["name"] for schema in registry.schemas()]
    assert names == sorted(names)
    assert names == ["query_airflow_runs", "query_warehouse"]
