"""The bounded investigating agent - happy paths, budgets, and the negative
security/error matrix (P0.6 10).

Uses `FakeModelClient` throughout so every scenario is deterministic and
offline, and the existing P0.4 test doubles (`tests/doubles.py`) for the tool
layer, so these tests are about the *agent's* behaviour, not the tool's - the
full real stack (real `query_warehouse`, real DuckDB) is exercised separately
in `tests/integration/test_inc001_agent_investigation.py`.
"""

from __future__ import annotations

import pytest
from pydantic import JsonValue

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
    Incident,
    RunState,
    Severity,
)
from causiq.errors import ModelContractError, ModelRefusalError, ModelTransientError
from causiq.ids import EvidenceId, FixedIdGenerator
from causiq.llm.fake_client import FakeModelClient
from causiq.llm.ports import ConversationMessage, ModelTurn, ToolCallBlock, ToolResultBlock
from causiq.tools import ToolRegistry
from tests.conftest import INCIDENT_ID, T0
from tests.doubles import EchoTool, ExplodingTool, HangingTool, MutatingTool

pytestmark = pytest.mark.unit


@pytest.fixture
def incident() -> Incident:
    return Incident(
        incident_id=INCIDENT_ID,
        title="revenue dropped",
        description="daily revenue is down 82%",
        severity=Severity.HIGH,
        detected_at=T0,
        detected_by="dq_monitor",
    )


@pytest.fixture
def registry() -> ToolRegistry:
    book = ToolRegistry()
    book.register(EchoTool())
    book.register(MutatingTool())
    return book


@pytest.fixture
def identity() -> AgentIdentity:
    return AgentIdentity(
        agent_id="agent_investigator_0",
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset({Permission.WAREHOUSE_READ}),
    )


def make_agent(
    *, model: FakeModelClient, registry: ToolRegistry, identity: AgentIdentity, clock: FrozenClock
) -> Investigator:
    return Investigator(
        model=model,
        registry=registry,
        identity=identity,
        id_generator=FixedIdGenerator(),
        clock=clock,
        system_prompt="test system prompt",
    )


def tool_call(call_id: str, tool_name: str, **arguments: JsonValue) -> ToolCallBlock:
    return ToolCallBlock(call_id=call_id, tool_name=tool_name, arguments=arguments)


def _evidence_id_from(history: tuple[ConversationMessage, ...]) -> str:
    """Find the evidence id the agent's own tool result put into history -
    the same thing a real model would read to know what to cite."""
    for message in history:
        for block in message.content:
            if isinstance(block, ToolResultBlock) and "evidence_id:" in block.content:
                line = next(
                    ln for ln in block.content.splitlines() if ln.startswith("evidence_id:")
                )
                return line.split(":", 1)[1].strip()
    raise AssertionError("no evidence_id found in history")


def final_citing(history: tuple[ConversationMessage, ...]) -> ModelTurn:
    """A scripted final turn that cites whatever evidence id is actually in
    history - never a hardcoded id, matching how the real agent test does it."""
    evidence_id = EvidenceId(_evidence_id_from(history))
    return ModelTurn(
        analysis=Analysis(
            outcome=AnalysisOutcome.COMPLETED,
            summary="found it",
            root_cause="the thing",
            confidence=Confidence.HIGH,
            claims=(Claim(statement="x explains y", citations=(evidence_id,)),),
        )
    )


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #
def test_full_investigation_with_one_tool_call_completes(
    incident: Incident, registry: ToolRegistry, identity: AgentIdentity
) -> None:
    model = FakeModelClient(
        [
            ModelTurn(tool_calls=(tool_call("c1", "echo_reader", value="hello", reason="check"),)),
            final_citing,
        ]
    )
    agent = make_agent(model=model, registry=registry, identity=identity, clock=FrozenClock(T0))
    sink = InMemoryAuditSink()

    run = agent.investigate(
        incident,
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60),
        audit_sink=sink,
    )

    assert run.state is RunState.COMPLETED
    assert run.analysis is not None
    assert run.analysis.root_cause == "the thing"
    assert len(run.evidence) == 1
    assert run.failure_reason is None
    assert run.ended_at is not None


def test_inconclusive_analysis_produces_inconclusive_run_state(
    incident: Incident, registry: ToolRegistry, identity: AgentIdentity
) -> None:
    model = FakeModelClient(
        [
            ModelTurn(
                analysis=Analysis(
                    outcome=AnalysisOutcome.INCONCLUSIVE,
                    summary="couldn't determine",
                    limitations="nothing conclusive",
                    confidence=Confidence.LOW,
                )
            )
        ]
    )
    agent = make_agent(model=model, registry=registry, identity=identity, clock=FrozenClock(T0))
    run = agent.investigate(
        incident,
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60),
        audit_sink=InMemoryAuditSink(),
    )
    assert run.state is RunState.INCONCLUSIVE
    assert run.analysis is not None


def test_multiple_tool_calls_in_one_turn_are_all_executed(
    incident: Incident, registry: ToolRegistry, identity: AgentIdentity
) -> None:
    model = FakeModelClient(
        [
            ModelTurn(
                tool_calls=(
                    tool_call("c1", "echo_reader", value="a", reason="r1"),
                    tool_call("c2", "echo_reader", value="b", reason="r2"),
                )
            ),
            ModelTurn(
                analysis=Analysis(
                    outcome=AnalysisOutcome.COMPLETED,
                    summary="s",
                    root_cause="rc",
                    confidence=Confidence.HIGH,
                    claims=(
                        Claim(
                            statement="x", citations=(EvidenceId("ev_001"), EvidenceId("ev_002"))
                        ),
                    ),
                )
            ),
        ]
    )
    agent = make_agent(model=model, registry=registry, identity=identity, clock=FrozenClock(T0))
    run = agent.investigate(
        incident,
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60),
        audit_sink=InMemoryAuditSink(),
    )
    assert run.state is RunState.COMPLETED
    assert len(run.evidence) == 2


def test_audit_trail_covers_the_full_lifecycle(
    incident: Incident, registry: ToolRegistry, identity: AgentIdentity
) -> None:
    model = FakeModelClient(
        [
            ModelTurn(tool_calls=(tool_call("c1", "echo_reader", value="hi", reason="r"),)),
            final_citing,
        ]
    )
    agent = make_agent(model=model, registry=registry, identity=identity, clock=FrozenClock(T0))
    sink = InMemoryAuditSink()
    agent.investigate(
        incident,
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60),
        audit_sink=sink,
    )
    events = sink.events()
    assert events[0] is AuditEventType.RUN_STARTED
    assert events[-1] is AuditEventType.RUN_ENDED
    assert AuditEventType.MODEL_CALL_REQUESTED in events
    assert AuditEventType.MODEL_CALL_COMPLETED in events
    assert AuditEventType.TOOL_AUTHORIZED in events
    assert AuditEventType.TOOL_EXECUTED in events
    assert AuditEventType.EVIDENCE_RECORDED in events
    assert AuditEventType.ANALYSIS_VALIDATED in events
    assert sink.closed is True


# --------------------------------------------------------------------------- #
# A. Fabricated citation
# --------------------------------------------------------------------------- #
def test_fabricated_citation_is_rejected(
    incident: Incident, registry: ToolRegistry, identity: AgentIdentity
) -> None:
    model = FakeModelClient(
        [
            ModelTurn(tool_calls=(tool_call("c1", "echo_reader", value="hi", reason="r"),)),
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
    agent = make_agent(model=model, registry=registry, identity=identity, clock=FrozenClock(T0))
    sink = InMemoryAuditSink()
    run = agent.investigate(
        incident,
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60),
        audit_sink=sink,
    )

    assert run.state is RunState.FAILED
    assert run.failure_reason is not None
    assert "ev_does_not_exist" in run.failure_reason
    # the (rejected) analysis is kept for forensic purposes, but the run is
    # NOT completed - a critical distinction, not an oversight.
    assert run.analysis is not None
    assert AuditEventType.ANALYSIS_REJECTED in sink.events()
    assert AuditEventType.ANALYSIS_VALIDATED not in sink.events()
    # the ledger genuinely only contains what the tool actually returned
    assert len(run.evidence) == 1
    assert run.evidence[0].evidence_id != "ev_does_not_exist"


# --------------------------------------------------------------------------- #
# B. Unauthorized tool call
# --------------------------------------------------------------------------- #
def test_unauthorized_tool_call_is_denied(incident: Incident, registry: ToolRegistry) -> None:
    powerless = AgentIdentity(
        agent_id="agent_powerless", role=AgentRole.INVESTIGATOR, permissions=frozenset()
    )
    model = FakeModelClient(
        [
            ModelTurn(tool_calls=(tool_call("c1", "echo_reader", value="hi", reason="r"),)),
            ModelTurn(
                analysis=Analysis(
                    outcome=AnalysisOutcome.INCONCLUSIVE,
                    summary="s",
                    limitations="could not query the warehouse",
                    confidence=Confidence.LOW,
                )
            ),
        ]
    )
    agent = make_agent(model=model, registry=registry, identity=powerless, clock=FrozenClock(T0))
    sink = InMemoryAuditSink()
    run = agent.investigate(
        incident,
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60),
        audit_sink=sink,
    )

    assert run.state is RunState.INCONCLUSIVE  # the agent adapted, as designed
    assert len(run.evidence) == 0
    assert AuditEventType.TOOL_DENIED in sink.events()
    assert AuditEventType.EVIDENCE_RECORDED not in sink.events()


def test_mutating_tool_call_is_denied_without_approval(
    incident: Incident, registry: ToolRegistry, identity: AgentIdentity
) -> None:
    """AC-8's guarantee, now exercised through the agent: an identity that
    does not hold warehouse.write cannot mutate by asking the model to try."""
    model = FakeModelClient(
        [
            ModelTurn(tool_calls=(tool_call("c1", "mutating_writer", target="x", reason="r"),)),
            ModelTurn(
                analysis=Analysis(
                    outcome=AnalysisOutcome.INCONCLUSIVE,
                    summary="s",
                    limitations="not authorized to mutate",
                    confidence=Confidence.LOW,
                )
            ),
        ]
    )
    agent = make_agent(model=model, registry=registry, identity=identity, clock=FrozenClock(T0))
    sink = InMemoryAuditSink()
    run = agent.investigate(
        incident,
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60),
        audit_sink=sink,
    )
    assert run.state is RunState.INCONCLUSIVE
    assert len(run.evidence) == 0
    assert (
        sink.entries[[e.event for e in sink.entries].index(AuditEventType.TOOL_DENIED)].detail[
            "reason"
        ]
        == "permission_not_granted"
    )


# --------------------------------------------------------------------------- #
# C. Tool failure
# --------------------------------------------------------------------------- #
def test_tool_failure_does_not_create_evidence_and_the_run_stays_bounded(
    incident: Incident, identity: AgentIdentity
) -> None:
    registry = ToolRegistry()
    registry.register(ExplodingTool())
    registry.register(EchoTool())
    model = FakeModelClient(
        [
            ModelTurn(tool_calls=(tool_call("c1", "exploding_reader", value="x", reason="r"),)),
            ModelTurn(
                analysis=Analysis(
                    outcome=AnalysisOutcome.INCONCLUSIVE,
                    summary="s",
                    limitations="the warehouse query failed",
                    confidence=Confidence.LOW,
                )
            ),
        ]
    )
    agent = make_agent(model=model, registry=registry, identity=identity, clock=FrozenClock(T0))
    sink = InMemoryAuditSink()
    run = agent.investigate(
        incident,
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60),
        audit_sink=sink,
    )
    assert run.state is RunState.INCONCLUSIVE
    assert len(run.evidence) == 0
    assert AuditEventType.TOOL_FAILED in sink.events()


# --------------------------------------------------------------------------- #
# D. Tool timeout
# --------------------------------------------------------------------------- #
def test_tool_timeout_produces_no_evidence_and_no_fabricated_success(
    incident: Incident, identity: AgentIdentity
) -> None:
    registry = ToolRegistry()
    registry.register(HangingTool(timeout_seconds=0.05))
    model = FakeModelClient(
        [
            ModelTurn(tool_calls=(tool_call("c1", "hanging_reader", value="x", reason="r"),)),
            ModelTurn(
                analysis=Analysis(
                    outcome=AnalysisOutcome.INCONCLUSIVE,
                    summary="s",
                    limitations="the warehouse query timed out",
                    confidence=Confidence.LOW,
                )
            ),
        ]
    )
    agent = make_agent(model=model, registry=registry, identity=identity, clock=FrozenClock(T0))
    sink = InMemoryAuditSink()
    run = agent.investigate(
        incident,
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60),
        audit_sink=sink,
    )
    assert run.state is RunState.INCONCLUSIVE
    assert len(run.evidence) == 0
    assert AuditEventType.TOOL_FAILED in sink.events()


# --------------------------------------------------------------------------- #
# E. Model exceeds turn budget - no infinite loop
# --------------------------------------------------------------------------- #
def test_turn_budget_exhaustion_terminates_without_calling_the_model_again(
    incident: Incident, registry: ToolRegistry, identity: AgentIdentity
) -> None:
    """The script is exactly `max_turns` long. If the loop were unbounded, it
    would call the fake a third time and hit "script exhausted" instead of a
    clean termination - this is the proof there is no infinite loop, not just
    an assertion that one particular run happened to stop."""
    model = FakeModelClient(
        [
            ModelTurn(tool_calls=(tool_call("c1", "echo_reader", value="a", reason="r"),)),
            ModelTurn(tool_calls=(tool_call("c2", "echo_reader", value="b", reason="r"),)),
        ]
    )
    agent = make_agent(model=model, registry=registry, identity=identity, clock=FrozenClock(T0))
    sink = InMemoryAuditSink()
    run = agent.investigate(
        incident,
        budget=Budget(
            max_turns=2, max_tool_calls=100, max_total_tokens=100_000, deadline_seconds=60
        ),
        audit_sink=sink,
    )

    assert run.state is RunState.BUDGET_EXCEEDED
    assert model.calls_made == 2
    assert model.remaining == 0  # both scripted turns were consumed, no more
    assert run.analysis is None
    assert run.failure_reason is not None
    assert run.failure_reason is not None
    assert "turn" in run.failure_reason
    assert AuditEventType.RUN_ENDED in sink.events()


# --------------------------------------------------------------------------- #
# F. Model exceeds tool-call budget
# --------------------------------------------------------------------------- #
def test_tool_call_budget_exhaustion_terminates_safely(
    incident: Incident, registry: ToolRegistry, identity: AgentIdentity
) -> None:
    model = FakeModelClient(
        [
            ModelTurn(tool_calls=(tool_call("c1", "echo_reader", value="a", reason="r"),)),
            ModelTurn(tool_calls=(tool_call("c2", "echo_reader", value="b", reason="r"),)),
            ModelTurn(tool_calls=(tool_call("c3", "echo_reader", value="c", reason="r"),)),
        ]
    )
    agent = make_agent(model=model, registry=registry, identity=identity, clock=FrozenClock(T0))
    sink = InMemoryAuditSink()
    run = agent.investigate(
        incident,
        budget=Budget(
            max_turns=100, max_tool_calls=2, max_total_tokens=100_000, deadline_seconds=60
        ),
        audit_sink=sink,
    )

    assert run.state is RunState.BUDGET_EXCEEDED
    assert len(run.evidence) == 2  # exactly the two calls the budget allowed
    assert run.analysis is None
    assert run.failure_reason is not None
    assert "tool call" in run.failure_reason
    assert AuditEventType.RUN_ENDED in sink.events()


def test_token_budget_exhausted_on_the_concluding_turn_discards_the_analysis(
    incident: Incident, registry: ToolRegistry, identity: AgentIdentity
) -> None:
    """`BudgetTracker.record_tokens` is charged *after* the model call returns
    (P0.3) - including for the turn that carries the final `Analysis`. If that
    turn's own tokens push the run over its ceiling, the budget must still
    win: the analysis is discarded entirely, never accepted as `COMPLETED`
    and never retained for forensics (unlike a citation rejection, which
    keeps the analysis on the run). The ceiling has no exception for output
    that was already fully paid for."""
    final_turn = ModelTurn(
        analysis=Analysis(
            outcome=AnalysisOutcome.INCONCLUSIVE,
            summary="s",
            limitations="not enough evidence",
            confidence=Confidence.LOW,
        ),
        output_tokens=1_000,
    )
    model = FakeModelClient([final_turn])
    agent = make_agent(model=model, registry=registry, identity=identity, clock=FrozenClock(T0))
    sink = InMemoryAuditSink()
    run = agent.investigate(
        incident,
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=500, deadline_seconds=60),
        audit_sink=sink,
    )

    assert run.state is RunState.BUDGET_EXCEEDED
    assert run.analysis is None
    assert run.failure_reason is not None
    assert "token" in run.failure_reason
    assert AuditEventType.ANALYSIS_VALIDATED not in sink.events()
    assert AuditEventType.RUN_ENDED in sink.events()


# --------------------------------------------------------------------------- #
# G. Malformed model response
# --------------------------------------------------------------------------- #
def test_malformed_model_response_terminates_without_bypassing_invariants(
    incident: Incident, registry: ToolRegistry, identity: AgentIdentity
) -> None:
    model = FakeModelClient([ModelContractError("the model returned garbage")])
    agent = make_agent(model=model, registry=registry, identity=identity, clock=FrozenClock(T0))
    sink = InMemoryAuditSink()
    run = agent.investigate(
        incident,
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60),
        audit_sink=sink,
    )
    assert run.state is RunState.FAILED
    assert run.analysis is None
    assert len(run.evidence) == 0
    assert AuditEventType.MODEL_CALL_FAILED in sink.events()


def test_model_refusal_terminates_the_run(
    incident: Incident, registry: ToolRegistry, identity: AgentIdentity
) -> None:
    model = FakeModelClient([ModelRefusalError("refused", category="cyber")])
    agent = make_agent(model=model, registry=registry, identity=identity, clock=FrozenClock(T0))
    run = agent.investigate(
        incident,
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60),
        audit_sink=InMemoryAuditSink(),
    )
    assert run.state is RunState.FAILED
    assert run.analysis is None


def test_transient_model_error_terminates_rather_than_retrying_forever(
    incident: Incident, registry: ToolRegistry, identity: AgentIdentity
) -> None:
    """P0.6 does not retry - a bounded run ends rather than looping on the
    model, and the script being exactly one entry long proves it: a retry
    would hit "script exhausted" instead."""
    model = FakeModelClient([ModelTransientError("rate limited")])
    agent = make_agent(model=model, registry=registry, identity=identity, clock=FrozenClock(T0))
    run = agent.investigate(
        incident,
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60),
        audit_sink=InMemoryAuditSink(),
    )
    assert run.state is RunState.FAILED
    assert model.calls_made == 1


# --------------------------------------------------------------------------- #
# Security: the model cannot reach the tool without the executor
# --------------------------------------------------------------------------- #
def test_agent_never_calls_a_tool_directly(
    incident: Incident, registry: ToolRegistry, identity: AgentIdentity
) -> None:
    """Structural, not just behavioural: the only tool double in this test
    that would prove a direct call happened is one that records its own
    invocations - and it must show zero when the identity lacks permission,
    even though the model asked for it."""
    tool = EchoTool()
    isolated_registry = ToolRegistry()
    isolated_registry.register(tool)
    powerless = AgentIdentity(
        agent_id="agent_powerless", role=AgentRole.INVESTIGATOR, permissions=frozenset()
    )
    model = FakeModelClient(
        [
            ModelTurn(tool_calls=(tool_call("c1", "echo_reader", value="x", reason="r"),)),
            ModelTurn(
                analysis=Analysis(
                    outcome=AnalysisOutcome.INCONCLUSIVE,
                    summary="s",
                    limitations="denied",
                    confidence=Confidence.LOW,
                )
            ),
        ]
    )
    agent = make_agent(
        model=model, registry=isolated_registry, identity=powerless, clock=FrozenClock(T0)
    )
    agent.investigate(
        incident,
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60),
        audit_sink=InMemoryAuditSink(),
    )
    assert tool.calls == []  # never reached, because AuthZ denied it first


def test_unknown_tool_request_is_handled_without_crashing(
    incident: Incident, registry: ToolRegistry, identity: AgentIdentity
) -> None:
    model = FakeModelClient(
        [
            ModelTurn(tool_calls=(tool_call("c1", "nonexistent_tool", reason="r"),)),
            ModelTurn(
                analysis=Analysis(
                    outcome=AnalysisOutcome.INCONCLUSIVE,
                    summary="s",
                    limitations="tool did not exist",
                    confidence=Confidence.LOW,
                )
            ),
        ]
    )
    agent = make_agent(model=model, registry=registry, identity=identity, clock=FrozenClock(T0))
    sink = InMemoryAuditSink()
    run = agent.investigate(
        incident,
        budget=Budget(max_turns=5, max_tool_calls=5, max_total_tokens=100_000, deadline_seconds=60),
        audit_sink=sink,
    )
    assert run.state is RunState.INCONCLUSIVE
    assert AuditEventType.TOOL_UNKNOWN in sink.events()
