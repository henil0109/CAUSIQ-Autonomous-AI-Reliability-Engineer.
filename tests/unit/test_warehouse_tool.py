"""`QueryWarehouseTool` - limits, cancellation, and the executor boundary.

Split from `test_sql_policy.py` (which tests the policy in isolation) and from
`tests/integration/test_inc001_investigation.py` (which uses this tool to
independently establish the seeded incident's cause). This file is about the
tool's own contract: bounded execution, and - most importantly - that the
*only* way its output ever becomes Evidence is through `ToolExecutor`.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from causiq.audit import AuditJournal, InMemoryAuditSink
from causiq.authz import AgentIdentity, Permission
from causiq.clock import FrozenClock
from causiq.domain import AgentRole, AuditEventType
from causiq.evidence import EvidenceLedger
from causiq.tools import ToolExecutor, ToolRegistry, ToolRequest, ToolResultStatus
from causiq.tools.warehouse import QueryWarehouseTool, WarehouseQueryInput
from tests.conftest import RUN_ID, T0

pytestmark = pytest.mark.unit


def build_executor(
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
        permissions=frozenset({Permission.WAREHOUSE_READ}),
    )


@pytest.fixture
def powerless_identity() -> AgentIdentity:
    return AgentIdentity(
        agent_id="agent_powerless", role=AgentRole.INVESTIGATOR, permissions=frozenset()
    )


def request(sql: str, *, reason: str = "investigate", tool_use_id: str = "toolu_01") -> ToolRequest:
    return ToolRequest(
        tool_use_id=tool_use_id,
        tool_name="query_warehouse",
        arguments={"sql": sql, "reason": reason},
    )


# --------------------------------------------------------------------------- #
# A. Happy path (direct tool call, complements the executor-integration tests)
# --------------------------------------------------------------------------- #
def test_simple_select_returns_expected_data(warehouse_db_path: Path) -> None:
    tool = QueryWarehouseTool(warehouse_db_path)
    output = tool.run(
        WarehouseQueryInput(sql="SELECT count(*) AS n FROM raw.orders", reason="sanity check")
    )
    data = json.loads(output.content)
    assert data["rows"] == [[630]]
    assert data["columns"] == [{"name": "n", "type": "BIGINT"}]
    assert data["truncated"] is False


def test_with_select_returns_expected_data(warehouse_db_path: Path) -> None:
    tool = QueryWarehouseTool(warehouse_db_path)
    output = tool.run(
        WarehouseQueryInput(
            sql="WITH t AS (SELECT * FROM raw.orders) SELECT count(*) FROM t",
            reason="cte check",
        )
    )
    assert json.loads(output.content)["rows"] == [[630]]


def test_content_records_the_executed_query_text(warehouse_db_path: Path) -> None:
    tool = QueryWarehouseTool(warehouse_db_path)
    output = tool.run(WarehouseQueryInput(sql="SELECT 1", reason="r"))
    assert json.loads(output.content)["sql"] == "SELECT 1"


def test_forbidden_sql_raises_rather_than_returning(warehouse_db_path: Path) -> None:
    from causiq.errors import ToolExecutionError

    tool = QueryWarehouseTool(warehouse_db_path)
    with pytest.raises(ToolExecutionError):
        tool.run(WarehouseQueryInput(sql="DROP TABLE raw.orders", reason="r"))


# --------------------------------------------------------------------------- #
# D. Limits - max rows
# --------------------------------------------------------------------------- #
def test_row_cap_truncates_and_flags(warehouse_db_path: Path) -> None:
    tool = QueryWarehouseTool(warehouse_db_path, max_rows=5)
    output = tool.run(WarehouseQueryInput(sql="SELECT * FROM raw.orders", reason="everything"))
    data = json.loads(output.content)
    assert output.truncated is True
    assert data["truncated"] is True
    assert data["row_count"] == 5
    assert len(data["rows"]) == 5


def test_result_under_the_row_cap_is_not_flagged_truncated(warehouse_db_path: Path) -> None:
    tool = QueryWarehouseTool(warehouse_db_path, max_rows=1000)
    output = tool.run(WarehouseQueryInput(sql="SELECT * FROM raw.orders", reason="everything"))
    assert output.truncated is False
    assert json.loads(output.content)["row_count"] == 630


def test_row_cap_boundary_is_exact(warehouse_db_path: Path) -> None:
    """Exactly `max_rows` results must not be flagged truncated - only MORE
    than the cap should be."""
    tool = QueryWarehouseTool(warehouse_db_path, max_rows=14)
    output = tool.run(WarehouseQueryInput(sql="SELECT * FROM analytics.revenue_daily", reason="r"))
    assert output.truncated is False
    assert json.loads(output.content)["row_count"] == 14


# --------------------------------------------------------------------------- #
# D. Limits - result byte size
# --------------------------------------------------------------------------- #
def test_byte_cap_drops_trailing_rows_and_flags_truncated(warehouse_db_path: Path) -> None:
    generous = QueryWarehouseTool(warehouse_db_path, max_rows=1000, max_result_bytes=1_000_000)
    full = json.loads(
        generous.run(WarehouseQueryInput(sql="SELECT * FROM raw.orders", reason="r")).content
    )
    assert full["row_count"] == 630

    tight = QueryWarehouseTool(warehouse_db_path, max_rows=1000, max_result_bytes=2_000)
    output = tight.run(WarehouseQueryInput(sql="SELECT * FROM raw.orders", reason="r"))
    data = json.loads(output.content)
    assert output.truncated is True
    assert data["truncated"] is True
    assert data["row_count"] < 630
    assert len(output.content.encode("utf-8")) <= 2_000 or data["row_count"] == 0


def test_byte_cap_result_never_exceeds_the_budget_by_much(warehouse_db_path: Path) -> None:
    """The final serialized content must respect the budget (allowing only for
    the fixed scaffold around zero rows, which cannot shrink further)."""
    tool = QueryWarehouseTool(warehouse_db_path, max_rows=1000, max_result_bytes=500)
    output = tool.run(WarehouseQueryInput(sql="SELECT * FROM raw.orders", reason="r"))
    data = json.loads(output.content)
    assert data["row_count"] >= 0
    if data["row_count"] > 0:
        assert len(output.content.encode("utf-8")) <= 500


def test_row_and_byte_truncation_both_set_the_content_flag(warehouse_db_path: Path) -> None:
    """Regression guard: the content's own `"truncated"` field must reflect a
    row-count truncation even when byte-shrinking never triggers."""
    tool = QueryWarehouseTool(warehouse_db_path, max_rows=3, max_result_bytes=1_000_000)
    output = tool.run(WarehouseQueryInput(sql="SELECT * FROM raw.orders", reason="r"))
    data = json.loads(output.content)
    assert data["truncated"] is True
    assert data["row_count"] == 3


# --------------------------------------------------------------------------- #
# D. Limits - SQL length (schema-level, before the tool even runs)
# --------------------------------------------------------------------------- #
def test_input_schema_rejects_sql_over_the_length_limit() -> None:
    from causiq.tools.sql_policy import MAX_SQL_LENGTH

    with pytest.raises(ValidationError):
        WarehouseQueryInput(sql="SELECT " + "1" * MAX_SQL_LENGTH, reason="r")


# --------------------------------------------------------------------------- #
# D/G. Timeout - a genuine, tested cancellation guarantee
# --------------------------------------------------------------------------- #
def test_slow_query_is_genuinely_cancelled_not_merely_abandoned(
    warehouse_db_path: Path,
) -> None:
    """Proves the claim, rather than asserting it: a query that would run far
    longer than the timeout is actually interrupted at the database layer -
    the call returns almost immediately, not after the full query would have
    finished."""
    from causiq.errors import ToolTimeoutError

    tool = QueryWarehouseTool(
        warehouse_db_path, query_timeout_seconds=0.3, outer_timeout_seconds=5.0
    )
    started = time.monotonic()
    with pytest.raises(ToolTimeoutError) as caught:
        tool.run(
            WarehouseQueryInput(
                sql="SELECT count(*) FROM range(200000000) a, range(50) b",
                reason="deliberately slow",
            )
        )
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, "the query was abandoned-and-waited-out, not cancelled"
    assert caught.value.recoverable is True
    assert caught.value.context["query_timeout_seconds"] == 0.3


def test_fast_query_is_not_affected_by_the_timeout(warehouse_db_path: Path) -> None:
    """No false positive: a query that finishes well inside the deadline must
    succeed normally, and the connection must not be left interrupted."""
    tool = QueryWarehouseTool(warehouse_db_path, query_timeout_seconds=2.0)
    output = tool.run(WarehouseQueryInput(sql="SELECT 1", reason="r"))
    assert json.loads(output.content)["rows"] == [[1]]


def test_connection_is_usable_immediately_after_a_timeout(warehouse_db_path: Path) -> None:
    """The interrupted connection is closed by `run()`'s own finally block;
    prove a FRESH call on the same tool still works, i.e. one timed-out query
    does not poison subsequent ones."""
    from causiq.errors import ToolTimeoutError

    tool = QueryWarehouseTool(
        warehouse_db_path, query_timeout_seconds=0.2, outer_timeout_seconds=5.0
    )
    with pytest.raises(ToolTimeoutError):
        tool.run(
            WarehouseQueryInput(
                sql="SELECT count(*) FROM range(200000000) a, range(50) b", reason="r"
            )
        )
    output = tool.run(WarehouseQueryInput(sql="SELECT 1", reason="r"))
    assert json.loads(output.content)["rows"] == [[1]]


def test_query_timeout_must_be_strictly_less_than_outer_timeout(
    warehouse_db_path: Path,
) -> None:
    """So the tool's own precise cancellation always fires before the
    executor's abandon-only backstop under normal conditions."""
    with pytest.raises(ValueError, match="strictly less than"):
        QueryWarehouseTool(warehouse_db_path, query_timeout_seconds=5.0, outer_timeout_seconds=5.0)
    with pytest.raises(ValueError, match="strictly less than"):
        QueryWarehouseTool(warehouse_db_path, query_timeout_seconds=6.0, outer_timeout_seconds=5.0)


def test_no_background_timer_survives_a_successful_query(warehouse_db_path: Path) -> None:
    """The `threading.Timer` backing the timeout is cancelled in `run()`'s
    `finally` block. Prove it leaves no trace - not just that the connection
    is reusable, but that the watchdog thread itself is gone, since a leaked
    timer thread is exactly the kind of resource leak that stays invisible
    until a long-running process accumulates enough of them to matter."""
    tool = QueryWarehouseTool(warehouse_db_path, query_timeout_seconds=2.0)
    tool.run(WarehouseQueryInput(sql="SELECT 1", reason="r"))
    time.sleep(0.05)  # let a cancelled-but-not-yet-joined timer thread exit
    assert not any(isinstance(t, threading.Timer) for t in threading.enumerate())


def test_no_background_timer_survives_a_timed_out_query(warehouse_db_path: Path) -> None:
    """Same property, on the path where the timer actually fires rather than
    being cancelled early - `con.interrupt()` running is not the same as the
    `threading.Timer` thread itself still being alive afterward."""
    from causiq.errors import ToolTimeoutError

    tool = QueryWarehouseTool(
        warehouse_db_path, query_timeout_seconds=0.2, outer_timeout_seconds=5.0
    )
    with pytest.raises(ToolTimeoutError):
        tool.run(
            WarehouseQueryInput(
                sql="SELECT count(*) FROM range(200000000) a, range(50) b", reason="r"
            )
        )
    time.sleep(0.1)
    assert not any(isinstance(t, threading.Timer) for t in threading.enumerate())


# --------------------------------------------------------------------------- #
# E. Architecture - Registry integration
# --------------------------------------------------------------------------- #
def test_tool_registers_cleanly(warehouse_db_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    assert "query_warehouse" in registry
    schema = registry.schemas()[0]
    assert schema["strict"] is True
    assert "reason" in schema["input_schema"]["required"]
    assert "sql" in schema["input_schema"]["required"]


# --------------------------------------------------------------------------- #
# E. Architecture - Executor integration: auth, evidence, audit
# --------------------------------------------------------------------------- #
def test_authorized_query_succeeds_through_the_executor(
    warehouse_db_path: Path, read_identity: AgentIdentity
) -> None:
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    ledger = EvidenceLedger(RUN_ID)
    sink = InMemoryAuditSink()
    executor = build_executor(registry=registry, ledger=ledger, sink=sink, identity=read_identity)

    result = executor.execute(request("SELECT count(*) FROM raw.orders"))

    assert result.status is ToolResultStatus.OK
    assert result.succeeded
    assert result.evidence_id == "ev_001"


def test_evidence_id_is_returned_and_present_in_the_ledger(
    warehouse_db_path: Path, read_identity: AgentIdentity
) -> None:
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    ledger = EvidenceLedger(RUN_ID)
    sink = InMemoryAuditSink()
    executor = build_executor(registry=registry, ledger=ledger, sink=sink, identity=read_identity)

    result = executor.execute(request("SELECT * FROM analytics.revenue_daily"))

    assert result.evidence_id is not None
    evidence = ledger.get(result.evidence_id)
    assert evidence is not None
    assert evidence.request.tool_name == "query_warehouse"
    assert evidence.request.arguments["sql"] == "SELECT * FROM analytics.revenue_daily"
    assert evidence.request.reason == "investigate"
    assert evidence.verify()
    assert f"evidence_id: {result.evidence_id}" in result.content


def test_audit_event_is_created_for_a_successful_query(
    warehouse_db_path: Path, read_identity: AgentIdentity
) -> None:
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    ledger = EvidenceLedger(RUN_ID)
    sink = InMemoryAuditSink()
    executor = build_executor(registry=registry, ledger=ledger, sink=sink, identity=read_identity)

    executor.execute(request("SELECT 1"))

    assert sink.events() == (
        AuditEventType.TOOL_AUTHORIZED,
        AuditEventType.TOOL_EXECUTED,
        AuditEventType.EVIDENCE_RECORDED,
    )


def test_authorization_is_enforced_through_the_executor(
    warehouse_db_path: Path, powerless_identity: AgentIdentity
) -> None:
    """AC-7/AC-8's guarantee, now proven against a REAL tool rather than a
    test double."""
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    ledger = EvidenceLedger(RUN_ID)
    sink = InMemoryAuditSink()
    executor = build_executor(
        registry=registry, ledger=ledger, sink=sink, identity=powerless_identity
    )

    result = executor.execute(request("SELECT 1"))

    assert result.status is ToolResultStatus.DENIED
    assert len(ledger) == 0
    assert sink.events() == (AuditEventType.TOOL_DENIED,)
    assert sink.entries[0].detail["reason"] == "permission_not_granted"


@pytest.mark.parametrize(
    ("label", "sql", "expected_status"),
    [
        ("forbidden statement", "DROP TABLE raw.orders", ToolResultStatus.FAILED),
        ("malformed sql", "SELECT FROM WHERE", ToolResultStatus.FAILED),
    ],
)
def test_denied_and_failed_queries_produce_no_evidence(
    label: str,
    sql: str,
    expected_status: ToolResultStatus,
    warehouse_db_path: Path,
    read_identity: AgentIdentity,
) -> None:
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    ledger = EvidenceLedger(RUN_ID)
    sink = InMemoryAuditSink()
    executor = build_executor(registry=registry, ledger=ledger, sink=sink, identity=read_identity)

    result = executor.execute(request(sql))

    assert result.status is expected_status, label
    assert result.is_error
    assert result.evidence_id is None
    assert len(ledger) == 0, label
    assert AuditEventType.EVIDENCE_RECORDED not in sink.events(), label


def test_invalid_input_produces_no_evidence(
    warehouse_db_path: Path, read_identity: AgentIdentity
) -> None:
    """Schema-level rejection (empty SQL) - never reaches the tool at all."""
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    ledger = EvidenceLedger(RUN_ID)
    sink = InMemoryAuditSink()
    executor = build_executor(registry=registry, ledger=ledger, sink=sink, identity=read_identity)

    result = executor.execute(
        ToolRequest(tool_use_id="t", tool_name="query_warehouse", arguments={"sql": ""})
    )

    assert result.status is ToolResultStatus.INVALID_INPUT
    assert len(ledger) == 0
    assert sink.events() == (AuditEventType.TOOL_AUTHORIZED, AuditEventType.TOOL_INPUT_REJECTED)


def test_timeout_through_the_executor_produces_no_evidence_and_is_audited(
    warehouse_db_path: Path, read_identity: AgentIdentity
) -> None:
    """The genuine, tool-level cancellation (`ToolTimeoutError`) surfaces
    through the executor as TIMEOUT with `mechanism: cancelled` in the audit
    detail - distinguishing it from the abandon-only outer bound."""
    registry = ToolRegistry()
    registry.register(
        QueryWarehouseTool(
            warehouse_db_path,
            name="query_warehouse",
            query_timeout_seconds=0.3,
            outer_timeout_seconds=5.0,
        )
    )
    ledger = EvidenceLedger(RUN_ID)
    sink = InMemoryAuditSink()
    executor = build_executor(registry=registry, ledger=ledger, sink=sink, identity=read_identity)

    result = executor.execute(request("SELECT count(*) FROM range(200000000) a, range(50) b"))

    assert result.status is ToolResultStatus.TIMEOUT
    assert result.is_error
    assert result.evidence_id is None
    assert len(ledger) == 0
    assert sink.events() == (AuditEventType.TOOL_AUTHORIZED, AuditEventType.TOOL_FAILED)
    assert sink.entries[-1].detail["mechanism"] == "cancelled"
    assert sink.entries[-1].detail["outcome"] == "timeout"


# --------------------------------------------------------------------------- #
# F. No direct-invocation bypass of the executor
# --------------------------------------------------------------------------- #
def test_direct_tool_invocation_cannot_produce_evidence_or_audit(
    warehouse_db_path: Path,
) -> None:
    """The structural proof, not a policy statement: `Tool.run(payload)` has no
    parameter through which a ledger or audit journal could ever be reached.
    Calling it directly - exactly what the model itself is never able to do,
    since it only ever emits a named `ToolRequest` for the registry and
    executor to interpret - returns a plain `ToolOutput` and nothing else.
    """
    tool = QueryWarehouseTool(warehouse_db_path)
    output = tool.run(WarehouseQueryInput(sql="SELECT 1", reason="direct call"))

    # It is a real, usable result...
    assert json.loads(output.content)["rows"] == [[1]]
    # ...but there is no ledger, no journal, no identity anywhere in this call.
    # `ToolOutput` itself carries no evidence id and no audit reference - the
    # only component in the codebase that can construct an `Evidence` record
    # is `EvidenceLedger.record`, and nothing here ever had a reference to one.
    assert not hasattr(output, "evidence_id")
    assert not hasattr(output, "audit_entry")


def test_run_has_no_ledger_or_journal_parameter() -> None:
    """A signature-level guarantee, not just a runtime observation."""
    import inspect

    signature = inspect.signature(QueryWarehouseTool.run)
    param_names = set(signature.parameters) - {"self"}
    assert param_names == {"payload"}


# --------------------------------------------------------------------------- #
# G. Determinism
# --------------------------------------------------------------------------- #
def test_repeated_identical_queries_produce_byte_identical_content(
    warehouse_db_path: Path,
) -> None:
    tool = QueryWarehouseTool(warehouse_db_path)
    first = tool.run(
        WarehouseQueryInput(sql="SELECT * FROM analytics.revenue_daily ORDER BY 1", reason="r")
    )
    second = tool.run(
        WarehouseQueryInput(sql="SELECT * FROM analytics.revenue_daily ORDER BY 1", reason="r")
    )
    assert first.content == second.content


def test_two_fresh_connections_see_the_same_read_only_data(
    warehouse_db_path: Path,
) -> None:
    """Two tool instances against the same file, run from different threads,
    must observe identical data - the connection-per-query design has no
    shared mutable state to race on."""
    tool_a = QueryWarehouseTool(warehouse_db_path)
    tool_b = QueryWarehouseTool(warehouse_db_path)
    results: dict[str, str] = {}

    def run_a() -> None:
        results["a"] = tool_a.run(
            WarehouseQueryInput(sql="SELECT count(*) FROM raw.orders", reason="r")
        ).content

    def run_b() -> None:
        results["b"] = tool_b.run(
            WarehouseQueryInput(sql="SELECT count(*) FROM raw.orders", reason="r")
        ).content

    t1, t2 = threading.Thread(target=run_a), threading.Thread(target=run_b)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results["a"] == results["b"]
