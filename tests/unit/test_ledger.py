"""Evidence ledger behaviour - invariant I2.

The ledger is append-only by construction. These tests assert both halves of
that: that the mutating methods do not exist, and that the ones that do exist
refuse to break the invariant.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from causiq.clock import FrozenClock
from causiq.domain import Evidence, EvidenceRequest, EvidenceSource
from causiq.errors import LedgerIntegrityError
from causiq.evidence import EvidenceLedger
from causiq.ids import EvidenceId, RunId
from tests.conftest import RUN_ID, T0

pytestmark = pytest.mark.unit


def _request(reason: str = "because") -> EvidenceRequest:
    return EvidenceRequest(
        tool_name="query_warehouse", arguments={"sql": "SELECT 1"}, reason=reason
    )


def test_ledger_mints_sequential_readable_ids(clock: FrozenClock) -> None:
    book = EvidenceLedger(RUN_ID)
    first = book.record(
        source=EvidenceSource.WAREHOUSE,
        agent_id="agent_a",
        request=_request(),
        content="[]",
        collected_at=clock.now(),
    )
    second = book.record(
        source=EvidenceSource.WAREHOUSE,
        agent_id="agent_a",
        request=_request(),
        content="[1]",
        collected_at=clock.now(),
    )
    assert first.evidence_id == "ev_001"
    assert second.evidence_id == "ev_002"


def test_ledger_exposes_no_mutation_api() -> None:
    """Append-only by construction, not by convention.

    If someone adds `delete` or `update` later, this test tells them they have
    changed the security model, not just the API.
    """
    for forbidden in ("delete", "remove", "update", "replace", "clear", "pop", "insert"):
        assert not hasattr(EvidenceLedger, forbidden), f"ledger must not expose {forbidden}()"


def test_snapshot_is_an_immutable_copy(ledger: EvidenceLedger) -> None:
    snapshot = ledger.snapshot()
    assert isinstance(snapshot, tuple)
    assert len(snapshot) == 2
    # Mutating the returned value cannot affect the ledger - it is a tuple.
    assert ledger.snapshot() == snapshot


def test_container_protocol(ledger: EvidenceLedger) -> None:
    assert len(ledger) == 2
    assert EvidenceId("ev_001") in ledger
    assert EvidenceId("ev_999") not in ledger
    assert [record.evidence_id for record in ledger] == ["ev_001", "ev_002"]
    assert ledger.ids() == {EvidenceId("ev_001"), EvidenceId("ev_002")}
    assert ledger.run_id == RUN_ID


def test_get_returns_none_for_unknown_id(ledger: EvidenceLedger) -> None:
    """A miss is a None, not an exception - callers routinely probe."""
    assert ledger.get(EvidenceId("ev_404")) is None
    found = ledger.get(EvidenceId("ev_001"))
    assert found is not None
    assert found.evidence_id == "ev_001"


def test_digest_is_computed_and_verifies(ledger: EvidenceLedger) -> None:
    for record in ledger:
        assert len(record.digest) == 64
        assert record.verify()
    ledger.verify_integrity()


def test_tampering_is_detected_by_digest(clock: FrozenClock) -> None:
    """Invariant I2: editing a record after collection is detectable."""
    book = EvidenceLedger(RUN_ID)
    original = book.record(
        source=EvidenceSource.WAREHOUSE,
        agent_id="agent_a",
        request=_request(),
        content='[{"rows": 4100}]',
        collected_at=clock.now(),
    )
    # Evidence is frozen, so tampering means constructing a substitute that keeps
    # the original digest while changing the content.
    forged = Evidence(
        **{**original.model_dump(), "content": '[{"rows": 99999}]'},
    )
    assert not forged.verify()

    tampered_book = EvidenceLedger(RUN_ID)
    with pytest.raises(LedgerIntegrityError, match="digest verification"):
        tampered_book.rehydrate((forged,))


def test_verify_integrity_raises_on_modified_record(clock: FrozenClock) -> None:
    book = EvidenceLedger(RUN_ID)
    original = book.record(
        source=EvidenceSource.WAREHOUSE,
        agent_id="agent_a",
        request=_request(),
        content="[]",
        collected_at=clock.now(),
    )
    forged = Evidence(**{**original.model_dump(), "content": "[999]"})
    # Reach past the append guard to simulate in-place corruption.
    book._records[0] = forged
    with pytest.raises(LedgerIntegrityError, match="digest mismatch"):
        book.verify_integrity()


def test_rejects_evidence_from_another_run(clock: FrozenClock) -> None:
    book = EvidenceLedger(RUN_ID)
    foreign = Evidence.create(
        evidence_id=EvidenceId("ev_001"),
        run_id=RunId("run_9999"),
        source=EvidenceSource.WAREHOUSE,
        agent_id="agent_a",
        request=_request(),
        content="[]",
        collected_at=clock.now(),
    )
    with pytest.raises(LedgerIntegrityError, match="different run"):
        book.rehydrate((foreign,))


def test_rejects_duplicate_evidence_id(clock: FrozenClock) -> None:
    book = EvidenceLedger(RUN_ID)
    record = Evidence.create(
        evidence_id=EvidenceId("ev_001"),
        run_id=RUN_ID,
        source=EvidenceSource.WAREHOUSE,
        agent_id="agent_a",
        request=_request(),
        content="[]",
        collected_at=clock.now(),
    )
    with pytest.raises(LedgerIntegrityError, match="already present"):
        book.rehydrate((record, record))


def test_rehydrate_restores_a_persisted_run(ledger: EvidenceLedger) -> None:
    restored = EvidenceLedger(RUN_ID)
    restored.rehydrate(ledger.snapshot())
    assert restored.snapshot() == ledger.snapshot()
    restored.verify_integrity()


def test_rehydrate_refuses_a_non_empty_ledger(ledger: EvidenceLedger) -> None:
    with pytest.raises(LedgerIntegrityError, match="already holds records"):
        ledger.rehydrate(ledger.snapshot())


def test_evidence_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Evidence.create(
            evidence_id=EvidenceId("ev_001"),
            run_id=RUN_ID,
            source=EvidenceSource.WAREHOUSE,
            agent_id="agent_a",
            request=_request(),
            content="[]",
            collected_at=datetime(2026, 9, 7, 12, 0, 0),  # noqa: DTZ001 - the point of the test
        )


def test_digest_changes_with_every_attested_field(clock: FrozenClock) -> None:
    """The digest must attest to provenance, not just content."""
    base: dict[str, Any] = {
        "run_id": RUN_ID,
        "source": EvidenceSource.WAREHOUSE,
        "agent_id": "agent_a",
        "request": _request(),
        "content": "[]",
        "content_type": "application/json",
        "collected_at": T0,
        "truncated": False,
    }
    baseline = Evidence.compute_digest(**base)
    variations: list[dict[str, Any]] = [
        {"agent_id": "agent_b"},
        {"content": "[1]"},
        {"truncated": True},
        {"content_type": "text/csv"},
        {"collected_at": T0 + timedelta(seconds=1)},
        {"request": _request("a different reason")},
        {"run_id": RunId("run_0002")},
    ]
    for change in variations:
        assert Evidence.compute_digest(**{**base, **change}) != baseline, change
