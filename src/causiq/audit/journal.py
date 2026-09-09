"""The audit journal and its sinks - invariant I7.

Maturity: hardened.

`AuditJournal` owns sequencing and timestamping; an `AuditSink` owns durability.
Splitting them means the JSONL file can become a database in Phase 7 without the
call sites changing, and means tests can assert on entries without touching a
filesystem.

Why every write flushes: the journal must survive the process that wrote it. A
run that crashes mid-investigation should still leave a record that reconstructs
how far it got - buffered writes would lose exactly the entries that matter most,
the ones just before the crash.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import IO, Protocol, Self, runtime_checkable

from pydantic import JsonValue

from causiq.clock import Clock
from causiq.domain.audit import AuditEntry, AuditEventType
from causiq.ids import RunId

#: Actor recorded for harness-level events that no agent performed.
SYSTEM_ACTOR = "system"


@runtime_checkable
class AuditSink(Protocol):
    """Port: durable destination for audit entries (ADR-0004)."""

    def emit(self, entry: AuditEntry) -> None: ...

    def close(self) -> None: ...


class InMemoryAuditSink:
    """Test implementation. Keeps entries in order, in memory."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []
        self.closed = False

    def emit(self, entry: AuditEntry) -> None:
        self.entries.append(entry)

    def close(self) -> None:
        self.closed = True

    def events(self) -> tuple[AuditEventType, ...]:
        """Event types in the order they were recorded."""
        return tuple(entry.event for entry in self.entries)


class JsonlAuditSink:
    """Production implementation: one JSON object per line, appended and flushed.

    JSONL rather than a single JSON document because a partially written array is
    unreadable, whereas a partially written JSONL file is a valid prefix of the
    truth - which is exactly what you want from a crashed run.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle: IO[str] = self._path.open("a", encoding="utf-8", newline="\n")

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, entry: AuditEntry) -> None:
        self._handle.write(entry.model_dump_json() + "\n")
        self._handle.flush()

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class AuditJournal:
    """Sequenced, timestamped audit recording for one run.

    Sequence numbers start at 1 and never skip. A reader that sees a gap knows a
    record was lost, which is the property that makes the journal trustworthy
    rather than merely present.
    """

    def __init__(self, run_id: RunId, clock: Clock, sink: AuditSink) -> None:
        self._run_id = run_id
        self._clock = clock
        self._sink = sink
        self._sequence = 0

    @property
    def run_id(self) -> RunId:
        return self._run_id

    @property
    def entries_written(self) -> int:
        return self._sequence

    def record(
        self,
        event: AuditEventType,
        *,
        actor: str = SYSTEM_ACTOR,
        **detail: JsonValue,
    ) -> AuditEntry:
        """Append one entry. Returns it so callers can log or assert on it."""
        self._sequence += 1
        entry = AuditEntry(
            sequence=self._sequence,
            run_id=self._run_id,
            at=self._clock.now(),
            event=event,
            actor=actor,
            detail=dict(detail),
        )
        self._sink.emit(entry)
        return entry

    def close(self) -> None:
        """Close the underlying sink. Always call this when a run ends."""
        self._sink.close()
