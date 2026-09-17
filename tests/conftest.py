"""Shared test fixtures.

Two properties this file is responsible for:

* **Offline by default (invariant I6).** Anything marked `live` is skipped unless
  `ANTHROPIC_API_KEY` is set. Skipped, never silently passed - a skip is visible
  in the report, a false pass is not.
* **Deterministic (AC-10).** Every fixture that needs time or identifiers uses
  `FrozenClock` and `FixedIdGenerator`, so two runs of the suite produce
  byte-identical artifacts.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from causiq.audit import AuditJournal, InMemoryAuditSink
from causiq.authz import AgentIdentity, ApprovalToken, Permission
from causiq.clock import FrozenClock
from causiq.domain import (
    AgentRole,
    Analysis,
    AnalysisOutcome,
    Budget,
    Claim,
    Confidence,
    EvidenceRequest,
    EvidenceSource,
    Incident,
    Severity,
)
from causiq.evidence import EvidenceLedger
from causiq.evidence_substrate import DEFAULT_SEED_SQL_PATH, build_warehouse_db
from causiq.ids import ApprovalId, IncidentId, RunId

#: Fixed origin for every deterministic test artifact.
T0 = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)

RUN_ID = RunId("run_0001")
INCIDENT_ID = IncidentId("inc_INC-001")


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip `live` tests unless an API key is present.

    Implemented as a collection hook rather than a decorator so the rule cannot
    be forgotten on a new live test.
    """
    del config
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return
    skip = pytest.mark.skip(reason="live test: ANTHROPIC_API_KEY is not set")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def clock() -> FrozenClock:
    """A clock that does not move unless a test moves it."""
    return FrozenClock(T0)


@pytest.fixture
def ticking_clock() -> FrozenClock:
    """A clock that advances one second per read, for ordered timestamps."""
    return FrozenClock(T0, step=timedelta(seconds=1))


@pytest.fixture
def identity() -> AgentIdentity:
    """The Phase 0 investigator: read-only, as invariant I4 requires by default."""
    return AgentIdentity(
        agent_id="agent_investigator_0",
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset({Permission.WAREHOUSE_READ}),
    )


@pytest.fixture
def write_identity() -> AgentIdentity:
    """An identity holding a write permission - used to prove that holding the
    permission is still not sufficient without an approval token (AC-8)."""
    return AgentIdentity(
        agent_id="agent_remediator_0",
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset({Permission.WAREHOUSE_READ, Permission.WAREHOUSE_WRITE}),
    )


@pytest.fixture
def approval() -> ApprovalToken:
    """A valid approval covering the `query_warehouse` resource."""
    return ApprovalToken(
        approval_id=ApprovalId("apr_0001"),
        granted_by="henil",
        scope="query_warehouse",
        granted_at=T0,
        expires_at=T0 + timedelta(minutes=30),
    )


@pytest.fixture
def incident() -> Incident:
    """INC-001 - the seeded revenue-drop incident from the Phase 0 plan."""
    return Incident(
        incident_id=INCIDENT_ID,
        title="revenue_daily total revenue dropped 82% on 2026-09-07",
        description=(
            "analytics.revenue_daily reports 82% lower total_revenue_usd for "
            "2026-09-07 with no failed pipeline task and no upstream row loss."
        ),
        severity=Severity.HIGH,
        detected_at=T0,
        detected_by="dq_monitor",
        affected_assets=("analytics.revenue_daily",),
    )


@pytest.fixture
def budget() -> Budget:
    """A small budget, so exhaustion tests are cheap to express."""
    return Budget(
        max_turns=3,
        max_tool_calls=5,
        max_total_tokens=1_000,
        deadline_seconds=60.0,
    )


@pytest.fixture
def ledger(identity: AgentIdentity, clock: FrozenClock) -> EvidenceLedger:
    """A ledger holding two real records, minted the only way evidence can be."""
    book = EvidenceLedger(RUN_ID)
    book.record(
        source=EvidenceSource.WAREHOUSE,
        agent_id=identity.agent_id,
        request=EvidenceRequest(
            tool_name="query_warehouse",
            arguments={"sql": "SELECT order_date, total_revenue_usd FROM analytics.revenue_daily"},
            reason="confirm the magnitude and date of the reported drop",
        ),
        content='[{"order_date":"2026-09-07","total_revenue_usd":18342.55}]',
        collected_at=clock.now(),
    )
    book.record(
        source=EvidenceSource.WAREHOUSE,
        agent_id=identity.agent_id,
        request=EvidenceRequest(
            tool_name="query_warehouse",
            arguments={"sql": "SELECT status, count(*) FROM raw.orders GROUP BY status"},
            reason="check whether a new order status appeared upstream",
        ),
        content='[{"status":"PENDING_CAPTURE","count":3380}]',
        collected_at=clock.now(),
    )
    return book


@pytest.fixture
def journal(clock: FrozenClock) -> tuple[AuditJournal, InMemoryAuditSink]:
    """A journal writing to an inspectable in-memory sink."""
    sink = InMemoryAuditSink()
    return AuditJournal(RUN_ID, clock, sink), sink


@pytest.fixture(scope="session")
def warehouse_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real INC-001 warehouse, built once for the whole test session.

    Session-scoped deliberately: building it is a real (if cheap) DuckDB
    operation, and it is never written to again after this fixture returns -
    every consumer opens it read-only, so sharing one file across tests is
    safe and keeps the suite fast. Individual tests that need an *empty* or
    *differently shaped* database build their own in a function-scoped
    tmp_path instead of using this fixture.
    """
    path = tmp_path_factory.mktemp("warehouse") / "inc001.duckdb"
    build_warehouse_db(path, seed_sql_path=DEFAULT_SEED_SQL_PATH)
    return path


def make_analysis(*, citations: tuple[str, ...]) -> Analysis:
    """A COMPLETED analysis citing exactly `citations`.

    A helper rather than a fixture because the fabricated-citation test (AC-6)
    needs to choose the ids.
    """
    return Analysis(
        outcome=AnalysisOutcome.COMPLETED,
        summary="Revenue under-reported because a new upstream order status is excluded.",
        root_cause=(
            "analytics.revenue_daily filters status = 'COMPLETED'; upstream began emitting "
            "PENDING_CAPTURE on 2026-09-07, so those orders are silently excluded."
        ),
        claims=(
            Claim(
                statement="total_revenue_usd fell 82% on 2026-09-07 while source volume held.",
                citations=tuple(citations),  # type: ignore[arg-type]
            ),
        ),
        confidence=Confidence.HIGH,
    )
