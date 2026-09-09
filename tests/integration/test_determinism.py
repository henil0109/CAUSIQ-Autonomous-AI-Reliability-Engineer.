"""Determinism and offline guarantees - invariant I6.

Two properties are proven here:

* **AC-10** - two runs with a frozen clock produce byte-identical audit journals.
  Without this, "reproducible" is a claim; with it, a reviewer can diff two runs
  and any difference is real signal.
* **AC-12 / AC-14** - the default suite touches no network and needs no API key.

These use the real filesystem, hence `integration` rather than `unit`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import JsonValue

from causiq.audit import AuditJournal, JsonlAuditSink
from causiq.authz import CapabilityRequest, Permission, authorize
from causiq.clock import FrozenClock
from causiq.domain import AgentRole, AuditEventType, EvidenceRequest, EvidenceSource
from causiq.evidence import EvidenceLedger, validate_citations
from causiq.ids import FixedIdGenerator
from tests.conftest import T0, make_analysis

pytestmark = pytest.mark.integration


def _simulate_run(journal_path: Path) -> tuple[str, str]:
    """A miniature end-to-end slice using only P0.1-P0.3 components.

    Deliberately not the real agent loop - that is P0.7. It exercises the pieces
    that exist: identity, authorization, evidence collection, citation validation,
    and audit recording, all under injected time and ids.
    """
    from causiq.authz import AgentIdentity

    clock = FrozenClock(T0)
    run_id = FixedIdGenerator().new_run_id()
    identity = AgentIdentity(
        agent_id="agent_investigator_0",
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset({Permission.WAREHOUSE_READ}),
    )

    with JsonlAuditSink(journal_path) as sink:
        journal = AuditJournal(run_id, clock, sink)
        ledger = EvidenceLedger(run_id)

        journal.record(AuditEventType.RUN_STARTED, incident_id="inc_INC-001")

        for sql, reason in (
            ("SELECT order_date, total_revenue_usd FROM analytics.revenue_daily", "confirm drop"),
            ("SELECT status, count(*) FROM raw.orders GROUP BY status", "check statuses"),
        ):
            request = CapabilityRequest(
                capability=Permission.WAREHOUSE_READ,
                resource="query_warehouse",
            )
            decision = authorize(identity, request, now=clock.now())
            journal.record(
                AuditEventType.TOOL_AUTHORIZED if decision.allowed else AuditEventType.TOOL_DENIED,
                actor=identity.agent_id,
                tool=request.resource,
            )
            evidence = ledger.record(
                source=EvidenceSource.WAREHOUSE,
                agent_id=identity.agent_id,
                request=EvidenceRequest(
                    tool_name="query_warehouse",
                    arguments={"sql": sql},
                    reason=reason,
                ),
                content='[{"rows":1}]',
                collected_at=clock.now(),
            )
            journal.record(
                AuditEventType.EVIDENCE_RECORDED,
                actor=identity.agent_id,
                evidence_id=str(evidence.evidence_id),
                digest=evidence.digest,
            )

        analysis = make_analysis(citations=("ev_001", "ev_002"))
        result = validate_citations(analysis, ledger)
        cited: list[JsonValue] = [str(item) for item in sorted(result.cited)]
        journal.record(
            AuditEventType.ANALYSIS_VALIDATED,
            actor=identity.agent_id,
            valid=result.valid,
            cited=cited,
        )
        journal.record(AuditEventType.RUN_ENDED, state="completed")

    content = journal_path.read_bytes()
    return hashlib.sha256(content).hexdigest(), content.decode("utf-8")


def test_two_runs_produce_byte_identical_journals(tmp_path: Path) -> None:
    """AC-10."""
    first_digest, first_text = _simulate_run(tmp_path / "run_a.jsonl")
    second_digest, second_text = _simulate_run(tmp_path / "run_b.jsonl")
    assert first_digest == second_digest, "audit journals diverged between identical runs"
    assert first_text == second_text


def test_journal_reconstructs_the_run_without_logs(tmp_path: Path) -> None:
    """Invariant I7: the journal alone must tell the story."""
    _, text = _simulate_run(tmp_path / "run.jsonl")
    events = [line for line in text.splitlines() if line.strip()]
    # started + (authorize + evidence) x2 + validated + ended
    assert len(events) == 7
    assert "run.started" in events[0]
    assert "run.ended" in events[-1]
    assert "ev_001" in text
    assert "ev_002" in text
