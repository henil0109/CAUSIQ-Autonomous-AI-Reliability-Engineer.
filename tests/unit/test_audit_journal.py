"""Audit journal behaviour - invariant I7, "every run is auditable"."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from causiq.audit import (
    SYSTEM_ACTOR,
    AuditJournal,
    AuditSink,
    InMemoryAuditSink,
    JsonlAuditSink,
)
from causiq.clock import FrozenClock
from causiq.domain import AuditEventType
from tests.conftest import RUN_ID, T0

pytestmark = pytest.mark.unit


def test_both_sinks_satisfy_the_port(tmp_path: Path) -> None:
    assert isinstance(InMemoryAuditSink(), AuditSink)
    sink = JsonlAuditSink(tmp_path / "run.jsonl")
    try:
        assert isinstance(sink, AuditSink)
    finally:
        sink.close()


def test_sequence_starts_at_one_and_never_skips(
    journal: tuple[AuditJournal, InMemoryAuditSink],
) -> None:
    """A gap in the sequence means a lost record, which a reader must be able
    to detect."""
    book, sink = journal
    for _ in range(5):
        book.record(AuditEventType.EVIDENCE_RECORDED, actor="agent_a")
    assert [entry.sequence for entry in sink.entries] == [1, 2, 3, 4, 5]
    assert book.entries_written == 5


def test_entries_carry_run_id_actor_and_time(
    journal: tuple[AuditJournal, InMemoryAuditSink],
) -> None:
    book, sink = journal
    entry = book.record(
        AuditEventType.TOOL_DENIED,
        actor="agent_investigator_0",
        tool="query_warehouse",
        reason="permission_not_granted",
    )
    assert entry.run_id == RUN_ID
    assert entry.actor == "agent_investigator_0"
    assert entry.at == T0
    assert entry.detail == {"tool": "query_warehouse", "reason": "permission_not_granted"}
    assert sink.entries == [entry]


def test_harness_events_default_to_the_system_actor(
    journal: tuple[AuditJournal, InMemoryAuditSink],
) -> None:
    book, _ = journal
    assert book.record(AuditEventType.RUN_STARTED).actor == SYSTEM_ACTOR


def test_timestamps_advance_with_the_clock(ticking_clock: FrozenClock) -> None:
    sink = InMemoryAuditSink()
    book = AuditJournal(RUN_ID, ticking_clock, sink)
    book.record(AuditEventType.RUN_STARTED)
    book.record(AuditEventType.RUN_ENDED)
    assert [entry.at for entry in sink.entries] == [T0, T0 + timedelta(seconds=1)]


def test_jsonl_sink_writes_one_object_per_line(tmp_path: Path, clock: FrozenClock) -> None:
    path = tmp_path / "nested" / "run_0001.jsonl"
    with JsonlAuditSink(path) as sink:
        book = AuditJournal(RUN_ID, clock, sink)
        book.record(AuditEventType.RUN_STARTED)
        book.record(AuditEventType.EVIDENCE_RECORDED, actor="agent_a", evidence_id="ev_001")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert records[0]["event"] == "run.started"
    assert records[1]["detail"]["evidence_id"] == "ev_001"


def test_jsonl_sink_flushes_each_write(tmp_path: Path, clock: FrozenClock) -> None:
    """A crashed run must still leave a readable prefix of the truth."""
    path = tmp_path / "run.jsonl"
    sink = JsonlAuditSink(path)
    try:
        AuditJournal(RUN_ID, clock, sink).record(AuditEventType.RUN_STARTED)
        # Not closed yet - the entry must already be on disk.
        assert path.read_text(encoding="utf-8").count("\n") == 1
    finally:
        sink.close()


def test_jsonl_sink_appends_rather_than_truncating(tmp_path: Path, clock: FrozenClock) -> None:
    path = tmp_path / "run.jsonl"
    with JsonlAuditSink(path) as sink:
        AuditJournal(RUN_ID, clock, sink).record(AuditEventType.RUN_STARTED)
    with JsonlAuditSink(path) as sink:
        AuditJournal(RUN_ID, clock, sink).record(AuditEventType.RUN_ENDED)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_closing_twice_is_safe(tmp_path: Path) -> None:
    sink = JsonlAuditSink(tmp_path / "run.jsonl")
    sink.close()
    sink.close()


def test_journal_close_closes_the_sink(journal: tuple[AuditJournal, InMemoryAuditSink]) -> None:
    book, sink = journal
    book.close()
    assert sink.closed


def test_in_memory_sink_reports_event_order(
    journal: tuple[AuditJournal, InMemoryAuditSink],
) -> None:
    book, sink = journal
    book.record(AuditEventType.RUN_STARTED)
    book.record(AuditEventType.ANALYSIS_REJECTED)
    book.record(AuditEventType.RUN_ENDED)
    assert sink.events() == (
        AuditEventType.RUN_STARTED,
        AuditEventType.ANALYSIS_REJECTED,
        AuditEventType.RUN_ENDED,
    )


def test_journal_exposes_its_run_id(journal: tuple[AuditJournal, InMemoryAuditSink]) -> None:
    book, _ = journal
    assert book.run_id == RUN_ID


def test_jsonl_sink_exposes_its_path(tmp_path: Path) -> None:
    """The runner reports the journal location to the operator at run end."""
    target = tmp_path / "run_0001.jsonl"
    with JsonlAuditSink(target) as sink:
        assert sink.path == target
