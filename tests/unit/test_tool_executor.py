"""The tool executor - the security boundary.

These are the most security-relevant tests in Phase 0. They assert the gates fire
in the right order, that every non-success outcome is audited and produces **no**
evidence, and that a success produces exactly one evidence record whose id the
model is actually told (without which it could not cite it, and the citation
validator would reject the analysis).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from causiq.audit import AuditJournal, InMemoryAuditSink
from causiq.authz import AgentIdentity, ApprovalToken, Permission
from causiq.clock import FrozenClock
from causiq.domain import AuditEventType, Budget, BudgetTracker
from causiq.errors import BudgetExceededError
from causiq.evidence import EvidenceLedger
from causiq.ids import ApprovalId
from causiq.obs import RecordingTracer, SpanName
from causiq.tools import ToolExecutor, ToolRegistry, ToolRequest, ToolResultStatus
from tests.conftest import RUN_ID, T0
from tests.doubles import EchoTool, ExplodingTool, HangingTool, MutatingTool

pytestmark = pytest.mark.unit


@pytest.fixture
def registry() -> ToolRegistry:
    book = ToolRegistry()
    book.register(EchoTool())
    book.register(MutatingTool())
    return book


@pytest.fixture
def sink() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def run_ledger() -> EvidenceLedger:
    return EvidenceLedger(RUN_ID)


def build_executor(
    *,
    registry: ToolRegistry,
    ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
    clock: FrozenClock | None = None,
    approvals: tuple[ApprovalToken, ...] = (),
    budget: BudgetTracker | None = None,
    tracer: RecordingTracer | None = None,
) -> ToolExecutor:
    the_clock = clock if clock is not None else FrozenClock(T0)
    return ToolExecutor(
        registry=registry,
        identity=identity,
        ledger=ledger,
        journal=AuditJournal(RUN_ID, the_clock, sink),
        clock=the_clock,
        tracer=tracer,
        budget=budget,
        approvals=approvals,
    )


def read_request(**arguments: object) -> ToolRequest:
    payload: dict[str, object] = {"value": "hello", "reason": "confirm the drop"}
    payload.update(arguments)
    return ToolRequest(tool_use_id="toolu_01", tool_name="echo_reader", arguments=payload)  # type: ignore[arg-type]


def write_request() -> ToolRequest:
    return ToolRequest(
        tool_use_id="toolu_02",
        tool_name="mutating_writer",
        arguments={"target": "analytics.revenue_daily", "reason": "backfill the aggregate"},
    )


# --------------------------------------------------------------------------- #
# Success path
# --------------------------------------------------------------------------- #
def test_successful_call_returns_ok(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    executor = build_executor(registry=registry, ledger=run_ledger, sink=sink, identity=identity)
    result = executor.execute(read_request(times=2))
    assert result.status is ToolResultStatus.OK
    assert result.is_error is False
    assert result.succeeded
    assert result.tool_use_id == "toolu_01"
    assert "hello hello" in result.content


def test_success_creates_exactly_one_evidence_record(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    executor = build_executor(registry=registry, ledger=run_ledger, sink=sink, identity=identity)
    result = executor.execute(read_request())

    assert len(run_ledger) == 1
    evidence = run_ledger.get(result.evidence_id)  # type: ignore[arg-type]
    assert evidence is not None
    assert evidence.evidence_id == "ev_001"
    assert evidence.agent_id == identity.agent_id
    assert evidence.request.tool_name == "echo_reader"
    assert evidence.request.reason == "confirm the drop"
    assert evidence.request.arguments == {"value": "hello", "times": 1}
    assert evidence.content == "hello"
    assert evidence.content_type == "text/plain"
    assert evidence.collected_at == T0
    assert evidence.verify()


def test_model_is_told_the_evidence_id_it_must_cite(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    """Without this, invariant I1 is unsatisfiable in practice.

    The model can only cite an id it has seen. If the executor recorded evidence
    but did not surface the id, every analysis would fail citation validation.
    """
    executor = build_executor(registry=registry, ledger=run_ledger, sink=sink, identity=identity)
    result = executor.execute(read_request())
    assert result.evidence_id == "ev_001"
    assert "evidence_id: ev_001" in result.content


def test_reason_is_carried_from_arguments_into_evidence(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    executor = build_executor(registry=registry, ledger=run_ledger, sink=sink, identity=identity)
    executor.execute(read_request(reason="rule out upstream row loss"))
    assert run_ledger.snapshot()[0].request.reason == "rule out upstream row loss"


def test_success_audits_authorization_execution_and_evidence(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    executor = build_executor(registry=registry, ledger=run_ledger, sink=sink, identity=identity)
    executor.execute(read_request())
    assert sink.events() == (
        AuditEventType.TOOL_AUTHORIZED,
        AuditEventType.TOOL_EXECUTED,
        AuditEventType.EVIDENCE_RECORDED,
    )
    recorded = sink.entries[-1]
    assert recorded.detail["evidence_id"] == "ev_001"
    assert recorded.detail["digest"] == run_ledger.snapshot()[0].digest
    assert recorded.actor == identity.agent_id


def test_multiple_calls_accumulate_sequential_evidence(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    executor = build_executor(registry=registry, ledger=run_ledger, sink=sink, identity=identity)
    first = executor.execute(read_request(value="one"))
    second = executor.execute(read_request(value="two"))
    assert (first.evidence_id, second.evidence_id) == ("ev_001", "ev_002")
    assert len(run_ledger) == 2


def test_execution_opens_the_declared_spans(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    """Phase 3 exports these; Phase 0 proves they are opened."""
    tracer = RecordingTracer()
    executor = build_executor(
        registry=registry, ledger=run_ledger, sink=sink, identity=identity, tracer=tracer
    )
    executor.execute(read_request())
    assert tracer.names() == (
        SpanName.TOOL_AUTHORIZE,
        SpanName.TOOL_EXECUTE,
        SpanName.EVIDENCE_RECORD,
    )
    assert all(span.closed for span in tracer.spans)


# --------------------------------------------------------------------------- #
# Unknown tool
# --------------------------------------------------------------------------- #
def test_unknown_tool_is_refused_without_evidence(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    executor = build_executor(registry=registry, ledger=run_ledger, sink=sink, identity=identity)
    result = executor.execute(
        ToolRequest(tool_use_id="toolu_09", tool_name="rm_rf", arguments={"reason": "r"})
    )
    assert result.status is ToolResultStatus.UNKNOWN_TOOL
    assert result.is_error
    assert result.evidence_id is None
    assert len(run_ledger) == 0
    assert sink.events() == (AuditEventType.TOOL_UNKNOWN,)


def test_unknown_tool_result_lists_the_real_tools(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    executor = build_executor(registry=registry, ledger=run_ledger, sink=sink, identity=identity)
    result = executor.execute(ToolRequest(tool_use_id="toolu_09", tool_name="rm_rf", arguments={}))
    assert "echo_reader" in result.content
    assert "mutating_writer" in result.content


def test_unknown_tool_never_reaches_authorization(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    """There is no capability to check for a tool that does not exist."""
    tracer = RecordingTracer()
    executor = build_executor(
        registry=registry, ledger=run_ledger, sink=sink, identity=identity, tracer=tracer
    )
    executor.execute(ToolRequest(tool_use_id="t", tool_name="rm_rf", arguments={}))
    assert SpanName.TOOL_AUTHORIZE not in tracer.names()


# --------------------------------------------------------------------------- #
# Authorization - invariant I4
# --------------------------------------------------------------------------- #
def test_read_is_allowed_for_a_read_identity(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    executor = build_executor(registry=registry, ledger=run_ledger, sink=sink, identity=identity)
    assert executor.execute(read_request()).succeeded


def test_denied_read_records_no_evidence(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
) -> None:
    """An identity holding nothing is denied even a read."""
    from causiq.domain import AgentRole

    powerless = AgentIdentity(
        agent_id="agent_powerless", role=AgentRole.INVESTIGATOR, permissions=frozenset()
    )
    executor = build_executor(registry=registry, ledger=run_ledger, sink=sink, identity=powerless)
    result = executor.execute(read_request())
    assert result.status is ToolResultStatus.DENIED
    assert result.evidence_id is None
    assert len(run_ledger) == 0
    assert sink.events() == (AuditEventType.TOOL_DENIED,)
    assert sink.entries[0].detail["reason"] == "permission_not_granted"


def test_write_denied_for_a_read_only_identity(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    executor = build_executor(registry=registry, ledger=run_ledger, sink=sink, identity=identity)
    result = executor.execute(write_request())
    assert result.status is ToolResultStatus.DENIED
    assert sink.entries[0].detail["reason"] == "permission_not_granted"
    assert len(run_ledger) == 0


def test_write_denied_without_approval_even_with_the_permission(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    write_identity: AgentIdentity,
) -> None:
    """AC-8, enforced through the executor rather than only through authz.

    `write_identity` genuinely holds `warehouse.write`. Holding the permission is
    not sufficient: a human must have approved this specific action.
    """
    assert write_identity.has(Permission.WAREHOUSE_WRITE)
    executor = build_executor(
        registry=registry, ledger=run_ledger, sink=sink, identity=write_identity
    )
    result = executor.execute(write_request())
    assert result.status is ToolResultStatus.DENIED
    assert sink.entries[0].detail["reason"] == "approval_required"
    assert sink.entries[0].detail["mutating"] is True
    assert len(run_ledger) == 0


def test_write_allowed_with_permission_and_valid_approval(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    write_identity: AgentIdentity,
) -> None:
    approval = ApprovalToken(
        approval_id=ApprovalId("apr_0001"),
        granted_by="henil",
        scope="mutating_writer",
        granted_at=T0,
        expires_at=T0 + timedelta(minutes=30),
    )
    executor = build_executor(
        registry=registry,
        ledger=run_ledger,
        sink=sink,
        identity=write_identity,
        approvals=(approval,),
    )
    result = executor.execute(write_request())
    assert result.status is ToolResultStatus.OK
    assert result.evidence_id == "ev_001"
    assert len(run_ledger) == 1


def test_expired_approval_is_denied(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    write_identity: AgentIdentity,
) -> None:
    approval = ApprovalToken(
        approval_id=ApprovalId("apr_0001"),
        granted_by="henil",
        scope="mutating_writer",
        granted_at=T0 - timedelta(hours=2),
        expires_at=T0 - timedelta(hours=1),
    )
    executor = build_executor(
        registry=registry,
        ledger=run_ledger,
        sink=sink,
        identity=write_identity,
        approvals=(approval,),
    )
    result = executor.execute(write_request())
    assert result.status is ToolResultStatus.DENIED
    assert sink.entries[0].detail["reason"] == "approval_expired"


def test_approval_for_another_tool_is_reported_as_a_scope_mismatch(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    write_identity: AgentIdentity,
) -> None:
    """The executor must not launder a wrong-scope token into "no approval"."""
    approval = ApprovalToken(
        approval_id=ApprovalId("apr_0002"),
        granted_by="henil",
        scope="some_other_tool",
        granted_at=T0,
        expires_at=T0 + timedelta(minutes=30),
    )
    executor = build_executor(
        registry=registry,
        ledger=run_ledger,
        sink=sink,
        identity=write_identity,
        approvals=(approval,),
    )
    result = executor.execute(write_request())
    assert result.status is ToolResultStatus.DENIED
    assert sink.entries[0].detail["reason"] == "approval_scope_mismatch"


def test_approval_does_not_substitute_for_a_missing_permission(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    """Otherwise approval becomes a privilege-escalation path."""
    approval = ApprovalToken(
        approval_id=ApprovalId("apr_0003"),
        granted_by="henil",
        scope="mutating_writer",
        granted_at=T0,
        expires_at=T0 + timedelta(minutes=30),
    )
    executor = build_executor(
        registry=registry,
        ledger=run_ledger,
        sink=sink,
        identity=identity,
        approvals=(approval,),
    )
    result = executor.execute(write_request())
    assert result.status is ToolResultStatus.DENIED
    assert sink.entries[0].detail["reason"] == "permission_not_granted"


def test_denied_call_never_runs_the_tool(
    run_ledger: EvidenceLedger, sink: InMemoryAuditSink, identity: AgentIdentity
) -> None:
    """The strongest statement of the boundary: the function is never entered."""
    tool = MutatingTool()
    book = ToolRegistry()
    book.register(tool)
    executor = build_executor(registry=book, ledger=run_ledger, sink=sink, identity=identity)
    executor.execute(write_request())
    assert tool.calls == []


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #
def test_invalid_arguments_are_rejected_without_evidence(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    executor = build_executor(registry=registry, ledger=run_ledger, sink=sink, identity=identity)
    result = executor.execute(
        ToolRequest(
            tool_use_id="toolu_03",
            tool_name="echo_reader",
            arguments={"value": "hello", "times": 99, "reason": "r"},
        )
    )
    assert result.status is ToolResultStatus.INVALID_INPUT
    assert result.evidence_id is None
    assert len(run_ledger) == 0
    assert sink.events() == (AuditEventType.TOOL_AUTHORIZED, AuditEventType.TOOL_INPUT_REJECTED)


def test_missing_required_reason_is_rejected(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    """The explainability field is enforced, not merely requested."""
    executor = build_executor(registry=registry, ledger=run_ledger, sink=sink, identity=identity)
    result = executor.execute(
        ToolRequest(tool_use_id="t", tool_name="echo_reader", arguments={"value": "hello"})
    )
    assert result.status is ToolResultStatus.INVALID_INPUT
    assert "reason" in result.content


def test_unexpected_argument_is_rejected(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    """`extra="forbid"` means a smuggled key fails rather than being ignored."""
    executor = build_executor(registry=registry, ledger=run_ledger, sink=sink, identity=identity)
    result = executor.execute(read_request(sql="DROP TABLE orders"))
    assert result.status is ToolResultStatus.INVALID_INPUT
    assert len(run_ledger) == 0


def test_validation_happens_after_authorization(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    """An unauthorized caller learns nothing about the tool's schema.

    The read-only identity sends malformed arguments to the write tool; it must be
    told it is denied, not told what the arguments should have been.
    """
    executor = build_executor(registry=registry, ledger=run_ledger, sink=sink, identity=identity)
    result = executor.execute(
        ToolRequest(tool_use_id="t", tool_name="mutating_writer", arguments={"nonsense": 1})
    )
    assert result.status is ToolResultStatus.DENIED
    assert sink.events() == (AuditEventType.TOOL_DENIED,)


# --------------------------------------------------------------------------- #
# Failure and timeout
# --------------------------------------------------------------------------- #
def test_tool_failure_returns_an_error_result_without_evidence(
    run_ledger: EvidenceLedger, sink: InMemoryAuditSink, identity: AgentIdentity
) -> None:
    book = ToolRegistry()
    book.register(ExplodingTool())
    executor = build_executor(registry=book, ledger=run_ledger, sink=sink, identity=identity)
    result = executor.execute(
        ToolRequest(
            tool_use_id="t",
            tool_name="exploding_reader",
            arguments={"value": "x", "reason": "r"},
        )
    )
    assert result.status is ToolResultStatus.FAILED
    assert result.is_error
    assert "connection reset" in result.content
    assert result.evidence_id is None
    assert len(run_ledger) == 0
    assert sink.events() == (AuditEventType.TOOL_AUTHORIZED, AuditEventType.TOOL_FAILED)


def test_tool_failure_does_not_end_the_run(
    run_ledger: EvidenceLedger, sink: InMemoryAuditSink, identity: AgentIdentity
) -> None:
    """Recoverable by classification: the executor returns rather than raising,
    so the agent can adapt - which is the point of an investigating agent."""
    book = ToolRegistry()
    book.register(ExplodingTool())
    book.register(EchoTool())
    executor = build_executor(registry=book, ledger=run_ledger, sink=sink, identity=identity)
    failed = executor.execute(
        ToolRequest(
            tool_use_id="t1",
            tool_name="exploding_reader",
            arguments={"value": "x", "reason": "r"},
        )
    )
    recovered = executor.execute(read_request())
    assert failed.is_error
    assert recovered.succeeded
    assert len(run_ledger) == 1


def test_timeout_abandons_the_call_without_evidence(
    run_ledger: EvidenceLedger, sink: InMemoryAuditSink, identity: AgentIdentity
) -> None:
    book = ToolRegistry()
    book.register(HangingTool())
    executor = build_executor(registry=book, ledger=run_ledger, sink=sink, identity=identity)
    result = executor.execute(
        ToolRequest(
            tool_use_id="t",
            tool_name="hanging_reader",
            arguments={"value": "x", "reason": "r"},
        )
    )
    assert result.status is ToolResultStatus.TIMEOUT
    assert result.is_error
    assert result.evidence_id is None
    assert len(run_ledger) == 0
    assert sink.events() == (AuditEventType.TOOL_AUTHORIZED, AuditEventType.TOOL_FAILED)
    assert sink.entries[-1].detail["outcome"] == "timeout"


# --------------------------------------------------------------------------- #
# Budget - invariant I5
# --------------------------------------------------------------------------- #
def test_budget_is_charged_per_call(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    clock = FrozenClock(T0)
    tracker = BudgetTracker(
        Budget(max_turns=5, max_tool_calls=2, max_total_tokens=1000, deadline_seconds=60.0),
        clock,
    )
    executor = build_executor(
        registry=registry,
        ledger=run_ledger,
        sink=sink,
        identity=identity,
        clock=clock,
        budget=tracker,
    )
    executor.execute(read_request())
    executor.execute(read_request())
    assert tracker.usage().tool_calls == 2


def test_budget_exhaustion_propagates_rather_than_returning(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    """The one condition that must stop the run rather than inform the model."""
    clock = FrozenClock(T0)
    tracker = BudgetTracker(
        Budget(max_turns=5, max_tool_calls=1, max_total_tokens=1000, deadline_seconds=60.0),
        clock,
    )
    executor = build_executor(
        registry=registry,
        ledger=run_ledger,
        sink=sink,
        identity=identity,
        clock=clock,
        budget=tracker,
    )
    executor.execute(read_request())
    with pytest.raises(BudgetExceededError, match="tool call budget"):
        executor.execute(read_request())
    assert len(run_ledger) == 1  # the refused call recorded nothing


def test_denied_calls_still_consume_budget(
    registry: ToolRegistry,
    run_ledger: EvidenceLedger,
    sink: InMemoryAuditSink,
    identity: AgentIdentity,
) -> None:
    """Otherwise an agent could probe denied tools without limit."""
    clock = FrozenClock(T0)
    tracker = BudgetTracker(
        Budget(max_turns=5, max_tool_calls=3, max_total_tokens=1000, deadline_seconds=60.0),
        clock,
    )
    executor = build_executor(
        registry=registry,
        ledger=run_ledger,
        sink=sink,
        identity=identity,
        clock=clock,
        budget=tracker,
    )
    executor.execute(write_request())
    assert tracker.usage().tool_calls == 1


# --------------------------------------------------------------------------- #
# Wiring safety and determinism
# --------------------------------------------------------------------------- #
def test_mismatched_ledger_and_journal_is_refused(
    registry: ToolRegistry, sink: InMemoryAuditSink, identity: AgentIdentity
) -> None:
    """Evidence written to one run's ledger and audited under another's id would
    be an unnoticed corruption of the audit trail."""
    from causiq.ids import RunId

    with pytest.raises(ValueError, match="different runs"):
        ToolExecutor(
            registry=registry,
            identity=identity,
            ledger=EvidenceLedger(RunId("run_9999")),
            journal=AuditJournal(RUN_ID, FrozenClock(T0), sink),
            clock=FrozenClock(T0),
        )


def test_identical_runs_produce_identical_evidence_digests(
    registry: ToolRegistry, identity: AgentIdentity
) -> None:
    """Invariant I6, at the executor level."""

    def run_once() -> str:
        ledger = EvidenceLedger(RUN_ID)
        executor = build_executor(
            registry=registry, ledger=ledger, sink=InMemoryAuditSink(), identity=identity
        )
        executor.execute(read_request())
        return ledger.snapshot()[0].digest

    assert run_once() == run_once()
