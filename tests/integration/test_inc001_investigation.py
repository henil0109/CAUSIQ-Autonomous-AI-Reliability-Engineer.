"""INC-001: independently establishing the planted incident through real SQL.

This is the test the whole of P0.5 exists to make possible. It runs the full,
real stack - `ToolRegistry` -> `ToolExecutor` -> authorization -> the real
`query_warehouse` tool -> a real DuckDB file - and then derives the causal
story entirely from the JSON *results* those queries returned. Nothing here
reads `fixtures/warehouse/seed.sql` or otherwise short-circuits to a known
answer: every assertion is computed from `EvidenceLedger` content, the same
place a Phase 1 investigator agent would have to look.

The five things this test establishes, each from its own piece of evidence:

  1. Source order VOLUME is healthy on the incident date (no data loss).
  2. PENDING_CAPTURE is absent before the incident date and appears on it.
  3. COMPLETED volume drops sharply on the incident date.
  4. analytics.revenue_daily's own numbers decline materially that day.
  5. revenue_daily is provably the COMPLETED-only slice of raw.orders - not
     merely correlated with it - which is what turns (1)-(4) into an
     explanation rather than a coincidence.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from causiq.audit import AuditJournal, InMemoryAuditSink
from causiq.authz import AgentIdentity, Permission
from causiq.clock import FrozenClock
from causiq.domain import (
    AgentRole,
    Analysis,
    AnalysisOutcome,
    AuditEventType,
    Claim,
    Confidence,
)
from causiq.evidence import EvidenceLedger, require_valid_citations
from causiq.evidence_substrate import load_incident_by_id
from causiq.ids import RunId
from causiq.tools import ToolExecutor, ToolRegistry, ToolRequest
from causiq.tools.warehouse import QueryWarehouseTool

pytestmark = pytest.mark.integration

INCIDENT_DATE = date(2026, 9, 7)
RUN_ID = RunId("run_inc001_investigation")


def _str(value: object) -> str:
    assert isinstance(value, str)
    return value


def _int(value: object) -> int:
    assert isinstance(value, int)
    return value


def _amount(value: object) -> float:
    """Decimal cells arrive as strings - `canonical_json`'s `default=str`
    fallback for values `json.dumps` cannot serialize natively - so this
    accepts either shape and asserts it is genuinely one of them."""
    assert isinstance(value, str | int | float)
    return float(value)


@pytest.fixture
def investigation(
    warehouse_db_path: Path,
) -> tuple[ToolExecutor, EvidenceLedger, InMemoryAuditSink]:
    """A fresh, real executor stack scoped to one investigation of INC-001."""
    registry = ToolRegistry()
    registry.register(QueryWarehouseTool(warehouse_db_path))
    identity = AgentIdentity(
        agent_id="agent_investigator_0",
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset({Permission.WAREHOUSE_READ}),
    )
    ledger = EvidenceLedger(RUN_ID)
    clock = FrozenClock()
    sink = InMemoryAuditSink()
    journal = AuditJournal(RUN_ID, clock, sink)
    executor = ToolExecutor(
        registry=registry, identity=identity, ledger=ledger, journal=journal, clock=clock
    )
    return executor, ledger, sink


def _query(executor: ToolExecutor, sql: str, reason: str, tool_use_id: str) -> list[list[object]]:
    """Issue one query through the real stack and return its evidence rows.

    Only `rows` is needed anywhere in this test, so that is all this helper
    returns - typed precisely, rather than an untyped `dict[str, object]`
    every call site would have to re-cast.
    """
    result = executor.execute(
        ToolRequest(
            tool_use_id=tool_use_id,
            tool_name="query_warehouse",
            arguments={"sql": sql, "reason": reason},
        )
    )
    assert result.succeeded, f"query failed: {result.content}"
    assert result.evidence_id is not None
    # The header the model would parse the evidence id from is present too.
    assert f"evidence_id: {result.evidence_id}" in result.content
    payload_start = result.content.index("{")
    payload: dict[str, object] = json.loads(result.content[payload_start:])
    rows = payload["rows"]
    assert isinstance(rows, list)
    return rows


def test_inc001_is_independently_established_through_sql(
    investigation: tuple[ToolExecutor, EvidenceLedger, InMemoryAuditSink],
) -> None:
    executor, ledger, sink = investigation

    # The incident record itself - loaded independently of the query results,
    # only to confirm the fixture we are investigating is the documented one.
    incident = load_incident_by_id("INC-001")
    assert "analytics.revenue_daily" in incident.affected_assets

    # --- Evidence 1: total order volume, all statuses, by day -------------
    volume = _query(
        executor,
        "SELECT CAST(order_ts AS DATE) AS order_date, count(*) AS order_count, "
        "sum(amount_usd) AS gross_amount FROM raw.orders GROUP BY 1 ORDER BY 1",
        reason="Confirm whether upstream order volume dropped on the incident date.",
        tool_use_id="toolu_volume",
    )
    volume_by_date = {_str(row[0]): (_int(row[1]), _amount(row[2])) for row in volume}
    incident_count, incident_gross = volume_by_date[str(INCIDENT_DATE)]
    prior_counts = [c for d, (c, _g) in volume_by_date.items() if d != str(INCIDENT_DATE)]
    prior_gross = [g for d, (_c, g) in volume_by_date.items() if d != str(INCIDENT_DATE)]

    # Fact 1: source volume is healthy - the incident day's order count and
    # gross amount are in line with every other day, not depressed.
    assert incident_count >= min(prior_counts) * 0.95
    assert incident_gross >= min(prior_gross) * 0.95

    # --- Evidence 2: status breakdown by day -------------------------------
    status_breakdown = _query(
        executor,
        "SELECT CAST(order_ts AS DATE) AS order_date, status, count(*) AS n "
        "FROM raw.orders GROUP BY 1, 2 ORDER BY 1, 2",
        reason="Check whether the mix of order statuses changed around the incident date.",
        tool_use_id="toolu_status",
    )
    by_date_status: dict[tuple[str, str], int] = {
        (_str(row[0]), _str(row[1])): _int(row[2]) for row in status_breakdown
    }
    pending_before = sum(
        n
        for (d, status), n in by_date_status.items()
        if status == "PENDING_CAPTURE" and d != str(INCIDENT_DATE)
    )
    pending_on_incident_day = by_date_status.get((str(INCIDENT_DATE), "PENDING_CAPTURE"), 0)
    completed_on_incident_day = by_date_status.get((str(INCIDENT_DATE), "COMPLETED"), 0)
    completed_prior_avg = (
        sum(
            n
            for (d, status), n in by_date_status.items()
            if status == "COMPLETED" and d != str(INCIDENT_DATE)
        )
        / 13
    )

    # Fact 2: PENDING_CAPTURE is new on the incident date.
    assert pending_before == 0
    assert pending_on_incident_day > 0

    # Fact 3: COMPLETED volume collapses on the incident date relative to the
    # established baseline.
    assert completed_on_incident_day < completed_prior_avg * 0.5

    # --- Evidence 3: the aggregate that is silently under-reporting -------
    revenue_daily = _query(
        executor,
        "SELECT * FROM analytics.revenue_daily ORDER BY order_date",
        reason="Read the reported daily revenue trend directly.",
        tool_use_id="toolu_revenue_daily",
    )
    revenue_by_date = {_str(row[0]): _amount(row[2]) for row in revenue_daily}
    incident_revenue = revenue_by_date[str(INCIDENT_DATE)]
    prior_revenue = [r for d, r in revenue_by_date.items() if d != str(INCIDENT_DATE)]
    baseline_revenue = min(prior_revenue)
    drop_ratio = (baseline_revenue - incident_revenue) / baseline_revenue

    # Fact 4: revenue materially declined - a large, unambiguous drop, not a
    # brittle exact-percentage assertion.
    assert drop_ratio > 0.5, f"expected a material revenue decline, observed {drop_ratio:.1%}"

    # --- Evidence 4: independently re-derive revenue_daily from raw.orders -
    recomputed = _query(
        executor,
        "SELECT CAST(order_ts AS DATE) AS order_date, count(*) AS completed_orders, "
        "sum(amount_usd) AS completed_revenue FROM raw.orders "
        "WHERE status = 'COMPLETED' GROUP BY 1 ORDER BY 1",
        reason="Test whether revenue_daily is exactly the COMPLETED-only slice of raw.orders.",
        tool_use_id="toolu_recompute",
    )
    recomputed_by_date = {_str(row[0]): _amount(row[2]) for row in recomputed}

    # Fact 5: the aggregate's own numbers match a fresh, independent
    # COMPLETED-only computation over raw.orders, for every date - proving the
    # filter is the mechanism, not merely correlated with the outcome.
    assert recomputed_by_date.keys() == revenue_by_date.keys()
    for order_date, reported in revenue_by_date.items():
        assert recomputed_by_date[order_date] == pytest.approx(reported, abs=0.01), order_date

    # --- The causal chain, stated explicitly ------------------------------
    # (1) total volume was healthy that day -> not a data-loss / pipeline
    #     failure; (2)+(3) the status mix shifted from COMPLETED to the new
    #     PENDING_CAPTURE status specifically on the incident date; (5) the
    #     aggregate is mathematically the COMPLETED-only slice of raw.orders.
    # Together these facts - and only these - explain fact (4): the revenue
    # decline is a semantic filter defect, not lost data.
    assert incident_count >= min(prior_counts) * 0.95  # (1) restated for the record
    assert pending_on_incident_day > 0 and pending_before == 0  # (2)
    assert completed_on_incident_day < completed_prior_avg * 0.5  # (3)
    assert recomputed_by_date[str(INCIDENT_DATE)] == pytest.approx(
        incident_revenue, abs=0.01
    )  # (5)

    assert len(ledger) == 4
    assert sink.events().count(AuditEventType.EVIDENCE_RECORDED) == 4


def test_the_collected_evidence_supports_a_fully_cited_analysis(
    investigation: tuple[ToolExecutor, EvidenceLedger, InMemoryAuditSink],
) -> None:
    """Closes the loop with invariant I1: evidence collected through the real
    P0.5 stack can support an `Analysis` whose citations all resolve.

    The analysis text below is hand-authored for this test, not produced by a
    model - P0.5 has no agent loop. What this proves is narrower and still
    real: the evidence the tool layer produces is *citable*, in the exact
    shape `causiq.evidence.require_valid_citations` demands.
    """
    executor, ledger, _sink = investigation

    e1 = executor.execute(
        ToolRequest(
            tool_use_id="t1",
            tool_name="query_warehouse",
            arguments={
                "sql": (
                    "SELECT CAST(order_ts AS DATE) AS d, count(*) FROM raw.orders "
                    "GROUP BY 1 ORDER BY 1"
                ),
                "reason": "confirm source volume",
            },
        )
    )
    e2 = executor.execute(
        ToolRequest(
            tool_use_id="t2",
            tool_name="query_warehouse",
            arguments={
                "sql": (
                    "SELECT CAST(order_ts AS DATE) AS d, status, count(*) FROM raw.orders "
                    "GROUP BY 1, 2 ORDER BY 1, 2"
                ),
                "reason": "check status distribution",
            },
        )
    )
    assert e1.evidence_id is not None
    assert e2.evidence_id is not None

    analysis = Analysis(
        outcome=AnalysisOutcome.COMPLETED,
        summary="Revenue under-reported because a new upstream status is excluded by the filter.",
        root_cause=(
            "analytics.revenue_daily aggregates only status = 'COMPLETED'. On 2026-09-07 "
            "most orders were classified PENDING_CAPTURE instead, while total order volume "
            "was unaffected, so the aggregate silently excludes real revenue."
        ),
        claims=(
            Claim(
                statement="Total order volume on 2026-09-07 was consistent with prior days.",
                citations=(e1.evidence_id,),
            ),
            Claim(
                statement="PENDING_CAPTURE appeared only on 2026-09-07, displacing COMPLETED.",
                citations=(e2.evidence_id,),
            ),
        ),
        confidence=Confidence.HIGH,
    )

    result = require_valid_citations(analysis, ledger)
    assert result.valid
    assert result.unresolved == frozenset()
