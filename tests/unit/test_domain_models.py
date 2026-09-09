"""Domain model behaviour: validation rules and round-trip serialization."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import BaseModel, ValidationError

from causiq.domain import (
    TERMINAL_RUN_STATES,
    Analysis,
    AnalysisOutcome,
    AuditEntry,
    AuditEventType,
    Budget,
    BudgetUsage,
    Claim,
    Confidence,
    Evidence,
    Hypothesis,
    HypothesisStatus,
    Incident,
    InvestigationRun,
    RunState,
    Severity,
)
from causiq.evidence import EvidenceLedger
from causiq.ids import EvidenceId, HypothesisId
from tests.conftest import INCIDENT_ID, RUN_ID, T0, make_analysis

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Round-trip - the persisted artifact must survive a save/load cycle exactly
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model_name", ["incident", "evidence", "analysis", "run"])
def test_round_trip_serialization(
    model_name: str,
    incident: Incident,
    ledger: EvidenceLedger,
    budget: Budget,
) -> None:
    analysis = make_analysis(citations=("ev_001",))
    run = InvestigationRun(
        run_id=RUN_ID,
        incident_id=INCIDENT_ID,
        agent_id="agent_investigator_0",
        state=RunState.COMPLETED,
        budget=budget,
        started_at=T0,
        ended_at=T0 + timedelta(seconds=30),
        evidence=ledger.snapshot(),
        analysis=analysis,
    )
    subjects: dict[str, tuple[BaseModel, type[BaseModel]]] = {
        "incident": (incident, Incident),
        "evidence": (ledger.snapshot()[0], Evidence),
        "analysis": (analysis, Analysis),
        "run": (run, InvestigationRun),
    }
    original, model_type = subjects[model_name]
    restored = model_type.model_validate_json(original.model_dump_json())
    assert restored == original


def test_evidence_survives_round_trip_with_a_verifiable_digest(ledger: EvidenceLedger) -> None:
    """Serialization must not silently break attribution (invariant I2)."""
    for record in ledger:
        restored = Evidence.model_validate_json(record.model_dump_json())
        assert restored.verify()


# --------------------------------------------------------------------------- #
# Incident
# --------------------------------------------------------------------------- #
def test_incident_rejects_naive_detected_at() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        Incident(
            incident_id=INCIDENT_ID,
            title="t",
            description="d",
            severity=Severity.LOW,
            detected_at=datetime(2026, 9, 7),  # noqa: DTZ001 - the point of the test
            detected_by="monitor",
        )


def test_incident_rejects_unknown_fields() -> None:
    """`extra="forbid"` catches a renamed field instead of silently ignoring it."""
    with pytest.raises(ValidationError):
        Incident(
            incident_id=INCIDENT_ID,
            title="t",
            description="d",
            severity=Severity.LOW,
            detected_at=T0,
            detected_by="monitor",
            sevrity="high",  # type: ignore[call-arg]
        )


def test_incident_is_frozen(incident: Incident) -> None:
    with pytest.raises(ValidationError):
        incident.title = "changed"


# --------------------------------------------------------------------------- #
# Claim / Analysis - the structural half of invariant I1
# --------------------------------------------------------------------------- #
def test_a_claim_cannot_be_uncited() -> None:
    """The schema makes an uncited claim inexpressible."""
    with pytest.raises(ValidationError):
        Claim(statement="revenue dropped", citations=())


def test_completed_analysis_requires_a_root_cause() -> None:
    with pytest.raises(ValidationError, match="must state a root_cause"):
        Analysis(
            outcome=AnalysisOutcome.COMPLETED,
            summary="s",
            claims=(Claim(statement="c", citations=(EvidenceId("ev_001"),)),),
            confidence=Confidence.HIGH,
        )


def test_completed_analysis_requires_at_least_one_claim() -> None:
    with pytest.raises(ValidationError, match="at least one cited claim"):
        Analysis(
            outcome=AnalysisOutcome.COMPLETED,
            summary="s",
            root_cause="rc",
            confidence=Confidence.HIGH,
        )


def test_inconclusive_analysis_must_not_assert_a_root_cause() -> None:
    with pytest.raises(ValidationError, match="must not state a root_cause"):
        Analysis(
            outcome=AnalysisOutcome.INCONCLUSIVE,
            summary="s",
            root_cause="a cause I cannot support",
            limitations="l",
            confidence=Confidence.LOW,
        )


def test_inconclusive_analysis_must_state_limitations() -> None:
    """Invariant I8: an honest exit has to say what stopped it."""
    with pytest.raises(ValidationError, match="must state its limitations"):
        Analysis(
            outcome=AnalysisOutcome.INCONCLUSIVE,
            summary="s",
            confidence=Confidence.LOW,
        )


def test_inconclusive_analysis_is_valid_with_limitations() -> None:
    analysis = Analysis(
        outcome=AnalysisOutcome.INCONCLUSIVE,
        summary="Could not determine the cause from warehouse data alone.",
        limitations="No access to the dbt model definition until Phase 1.",
        confidence=Confidence.LOW,
        recommended_next_steps=("Inspect the revenue_daily model SQL",),
    )
    assert analysis.outcome is AnalysisOutcome.INCONCLUSIVE
    assert analysis.cited_evidence() == frozenset()


# --------------------------------------------------------------------------- #
# Hypothesis
# --------------------------------------------------------------------------- #
def test_supported_hypothesis_requires_supporting_evidence() -> None:
    with pytest.raises(ValidationError, match="supporting evidence"):
        Hypothesis(
            hypothesis_id=HypothesisId("hyp_001"),
            statement="upstream changed",
            status=HypothesisStatus.SUPPORTED,
        )


def test_refuted_hypothesis_requires_refuting_evidence() -> None:
    with pytest.raises(ValidationError, match="refuting evidence"):
        Hypothesis(
            hypothesis_id=HypothesisId("hyp_001"),
            statement="rows were lost",
            status=HypothesisStatus.REFUTED,
        )


def test_proposed_hypothesis_needs_no_evidence_yet() -> None:
    hypothesis = Hypothesis(
        hypothesis_id=HypothesisId("hyp_001"),
        statement="a new status value appeared upstream",
    )
    assert hypothesis.status is HypothesisStatus.PROPOSED
    assert hypothesis.cited_evidence() == frozenset()


def test_hypothesis_cited_evidence_merges_both_sides() -> None:
    hypothesis = Hypothesis(
        hypothesis_id=HypothesisId("hyp_002"),
        statement="mixed",
        status=HypothesisStatus.SUPPORTED,
        supporting_evidence=(EvidenceId("ev_001"),),
        refuting_evidence=(EvidenceId("ev_002"),),
    )
    assert hypothesis.cited_evidence() == {EvidenceId("ev_001"), EvidenceId("ev_002")}


# --------------------------------------------------------------------------- #
# InvestigationRun - invariant I8's "recorded terminal state"
# --------------------------------------------------------------------------- #
def _run(**overrides: object) -> InvestigationRun:
    base: dict[str, object] = {
        "run_id": RUN_ID,
        "incident_id": INCIDENT_ID,
        "agent_id": "agent_investigator_0",
        "state": RunState.RUNNING,
        "budget": Budget(
            max_turns=3, max_tool_calls=5, max_total_tokens=1000, deadline_seconds=60.0
        ),
        "started_at": T0,
    }
    return InvestigationRun(**{**base, **overrides})  # type: ignore[arg-type]


def test_terminal_run_requires_an_end_time() -> None:
    with pytest.raises(ValidationError, match="must have ended_at"):
        _run(state=RunState.INCONCLUSIVE)


def test_non_terminal_run_must_not_have_an_end_time() -> None:
    with pytest.raises(ValidationError, match="must not have ended_at"):
        _run(state=RunState.RUNNING, ended_at=T0)


def test_completed_run_must_carry_its_analysis() -> None:
    with pytest.raises(ValidationError, match="must carry its analysis"):
        _run(state=RunState.COMPLETED, ended_at=T0 + timedelta(seconds=5))


def test_end_time_may_not_precede_start_time() -> None:
    with pytest.raises(ValidationError, match="must not precede"):
        _run(state=RunState.FAILED, ended_at=T0 - timedelta(seconds=1))


def test_budget_exceeded_is_a_valid_terminal_outcome(ledger: EvidenceLedger) -> None:
    """Invariant I8: running out of budget is an outcome, not an exception."""
    run = _run(
        state=RunState.BUDGET_EXCEEDED,
        ended_at=T0 + timedelta(seconds=60),
        evidence=ledger.snapshot(),
        failure_reason="turn budget exhausted",
    )
    assert run.is_terminal
    assert run.analysis is None
    assert run.evidence_ids() == {"ev_001", "ev_002"}


def test_running_run_is_not_terminal() -> None:
    assert not _run().is_terminal
    assert RunState.RUNNING not in TERMINAL_RUN_STATES
    assert RunState.PENDING not in TERMINAL_RUN_STATES


@pytest.mark.parametrize("state", sorted(TERMINAL_RUN_STATES))
def test_every_terminal_state_is_reachable(state: RunState, ledger: EvidenceLedger) -> None:
    analysis = make_analysis(citations=("ev_001",)) if state is RunState.COMPLETED else None
    run = _run(state=state, ended_at=T0 + timedelta(seconds=1), analysis=analysis)
    assert run.is_terminal


def test_run_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _run(started_at=datetime(2026, 9, 7))  # noqa: DTZ001 - the point of the test


# --------------------------------------------------------------------------- #
# Audit entry
# --------------------------------------------------------------------------- #
def test_audit_entry_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        AuditEntry(
            sequence=1,
            run_id=RUN_ID,
            at=datetime(2026, 9, 7),  # noqa: DTZ001 - the point of the test
            event=AuditEventType.RUN_STARTED,
            actor="system",
        )


def test_audit_sequence_starts_at_one() -> None:
    with pytest.raises(ValidationError):
        AuditEntry(
            sequence=0,
            run_id=RUN_ID,
            at=T0,
            event=AuditEventType.RUN_STARTED,
            actor="system",
        )


def test_budget_usage_totals_tokens() -> None:
    usage = BudgetUsage(input_tokens=120, output_tokens=30)
    assert usage.total_tokens == 150
    assert BudgetUsage().total_tokens == 0
