"""`DbtRunResultsTool`, through the real capability layer (P1.2).

Mirrors `test_airflow_evidence.py`'s discipline: real `ToolRegistry`, real
`ToolExecutor`, real `authz.authorize`, real `EvidenceLedger`, real
`AuditJournal` - nothing about the security/evidence boundary is mocked.
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
from causiq.tools.dbt import DbtRunResultsTool
from causiq.tools.warehouse import QueryWarehouseTool
from tests.conftest import RUN_ID, T0

pytestmark = pytest.mark.integration


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(DbtRunResultsTool())
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
        tool_name="query_dbt_run_results",
        arguments={
            "unique_id": "model.revenue_analytics.daily_revenue_pipeline",
            "reason": "check dbt run history",
        },
    )
    result = executor.execute(request)

    assert result.status is ToolResultStatus.OK
    assert result.is_error is False
    assert result.evidence_id is not None

    assert len(ledger) == 1
    evidence = ledger.snapshot()[0]
    assert evidence.verify()
    assert evidence.source == EvidenceSource.DBT
    assert evidence.agent_id == "agent_investigator_0"
    assert evidence.request.tool_name == "query_dbt_run_results"
    assert evidence.request.arguments == {
        "unique_id": "model.revenue_analytics.daily_revenue_pipeline"
    }
    assert evidence.request.reason == "check dbt run history"

    parsed_content = json.loads(evidence.content)
    assert parsed_content["unique_id"] == "model.revenue_analytics.daily_revenue_pipeline"

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
        tool_name="query_dbt_run_results",
        arguments={"unique_id": "model.revenue_analytics.stg_orders", "reason": "r"},
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
        tool_name="query_dbt_run_results",
        arguments={"unique_id": "model.revenue_analytics.daily_revenue_pipeline", "reason": "r"},
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
# C. Unknown model -> ordinary failure, no evidence
# --------------------------------------------------------------------------- #
def test_unknown_model_fails_without_creating_evidence(read_identity: AgentIdentity) -> None:
    ledger = EvidenceLedger(RUN_ID)
    sink = InMemoryAuditSink()
    executor = _executor(registry=_registry(), ledger=ledger, sink=sink, identity=read_identity)

    request = ToolRequest(
        tool_use_id="t1",
        tool_name="query_dbt_run_results",
        arguments={"unique_id": "model.pkg.not_a_real_model", "reason": "r"},
    )
    result = executor.execute(request)

    assert result.status is ToolResultStatus.FAILED
    assert result.is_error is True
    assert result.evidence_id is None
    assert len(ledger) == 0

    events = sink.events()
    assert AuditEventType.TOOL_FAILED in events
    assert AuditEventType.EVIDENCE_RECORDED not in events


def test_malformed_unique_id_is_rejected_as_invalid_input_not_a_failure(
    read_identity: AgentIdentity,
) -> None:
    """A path-traversal-shaped argument never reaches `DbtRunResultsTool.run` -
    the executor's own schema validation (Gate 3) rejects it first."""
    ledger = EvidenceLedger(RUN_ID)
    sink = InMemoryAuditSink()
    executor = _executor(registry=_registry(), ledger=ledger, sink=sink, identity=read_identity)

    request = ToolRequest(
        tool_use_id="t1",
        tool_name="query_dbt_run_results",
        arguments={"unique_id": "../../etc/passwd", "reason": "r"},
    )
    result = executor.execute(request)

    assert result.status is ToolResultStatus.INVALID_INPUT
    assert result.evidence_id is None
    assert len(ledger) == 0
    assert AuditEventType.TOOL_INPUT_REJECTED in sink.events()


# --------------------------------------------------------------------------- #
# Warehouse + Airflow + dbt coexistence (the real runner shape)
# --------------------------------------------------------------------------- #
def test_warehouse_airflow_and_dbt_tools_coexist_in_one_registry(warehouse_db_path: Path) -> None:
    """Sanity check matching `runner.build_registry()`'s actual shape: all
    three tools registered together, each still attributing evidence to its
    own `EvidenceSource`, with an identity holding every read permission able
    to use all three."""
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    registry.register(AirflowDagRunsTool())
    registry.register(DbtRunResultsTool())
    assert registry.names() == (
        "query_airflow_runs",
        "query_dbt_run_results",
        "query_warehouse",
    )

    identity = AgentIdentity(
        agent_id="agent_investigator_0",
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset({Permission.WAREHOUSE_READ, Permission.ARTIFACTS_READ}),
    )
    ledger = EvidenceLedger(RUN_ID)
    executor = _executor(
        registry=registry, ledger=ledger, sink=InMemoryAuditSink(), identity=identity
    )

    dbt_result = executor.execute(
        ToolRequest(
            tool_use_id="t1",
            tool_name="query_dbt_run_results",
            arguments={
                "unique_id": "model.revenue_analytics.daily_revenue_pipeline",
                "reason": "r",
            },
        )
    )
    airflow_result = executor.execute(
        ToolRequest(
            tool_use_id="t2",
            tool_name="query_airflow_runs",
            arguments={"dag_id": "daily_revenue_pipeline", "reason": "r"},
        )
    )
    warehouse_result = executor.execute(
        ToolRequest(
            tool_use_id="t3",
            tool_name="query_warehouse",
            arguments={"sql": "SELECT count(*) FROM raw.orders", "reason": "r"},
        )
    )

    assert dbt_result.status is ToolResultStatus.OK
    assert airflow_result.status is ToolResultStatus.OK
    assert warehouse_result.status is ToolResultStatus.OK
    assert len(ledger) == 3
    sources = {evidence.source for evidence in ledger.snapshot()}
    assert sources == {EvidenceSource.DBT, EvidenceSource.AIRFLOW, EvidenceSource.WAREHOUSE}


def test_registry_schema_digest_is_stable_across_calls_with_all_three_tools(
    warehouse_db_path: Path,
) -> None:
    """Adding dbt must not make the emitted tool list unstable - the same
    `schema_digest` property already proven for one and two tools must still
    hold with three (Engineering Contract 6.3)."""
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    registry.register(AirflowDagRunsTool())
    registry.register(DbtRunResultsTool())

    first_digest = registry.schema_digest()
    second_digest = registry.schema_digest()
    assert first_digest == second_digest

    first_schemas = registry.schemas()
    second_schemas = registry.schemas()
    assert first_schemas == second_schemas


def test_adding_dbt_does_not_change_the_other_tools_own_schemas(warehouse_db_path: Path) -> None:
    """Registering a third tool must not perturb the other two tools' own
    emitted schemas - each tool's schema is a pure function of that tool
    alone, never of what else happens to share the registry."""
    solo_warehouse_schema = tool_schema(QueryWarehouseTool(warehouse_db_path))
    solo_airflow_schema = tool_schema(AirflowDagRunsTool())

    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    registry.register(AirflowDagRunsTool())
    registry.register(DbtRunResultsTool())
    schemas_by_name = {schema["name"]: schema for schema in registry.schemas()}

    assert schemas_by_name["query_warehouse"] == solo_warehouse_schema
    assert schemas_by_name["query_airflow_runs"] == solo_airflow_schema


def test_registry_emits_tools_in_deterministic_name_sorted_order(warehouse_db_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    registry.register(AirflowDagRunsTool())
    registry.register(DbtRunResultsTool())
    names = [schema["name"] for schema in registry.schemas()]
    assert names == sorted(names)
    assert names == ["query_airflow_runs", "query_dbt_run_results", "query_warehouse"]
