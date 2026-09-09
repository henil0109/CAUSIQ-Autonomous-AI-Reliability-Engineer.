"""Audit records - invariant I7.

Maturity: hardened.

The audit journal is the answer to "what did the agent actually do", and it must
be answerable from persisted records alone, without reading application logs and
without re-running the agent. That makes it evidence about the *system*, in the
same way the ledger is evidence about the *incident*.

Entries are append-only and sequence-numbered. The sequence is what lets a
reviewer detect a gap: a journal that jumps from 7 to 9 is a journal that lost a
record, and silently losing an audit record is worse than crashing.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from causiq.ids import RunId


class AuditEventType(StrEnum):
    """Everything worth reconstructing after the fact.

    Phase 0 emits the run, authorization, evidence and analysis events. The model
    and tool-execution events are declared now because the taxonomy should be
    stable before the agent loop lands in P0.6/P0.7 - adding a member later is
    fine, renaming one breaks every stored journal.
    """

    RUN_STARTED = "run.started"
    RUN_ENDED = "run.ended"
    MODEL_CALL_REQUESTED = "model.call.requested"
    MODEL_CALL_COMPLETED = "model.call.completed"
    MODEL_CALL_FAILED = "model.call.failed"
    TOOL_AUTHORIZED = "tool.authorized"
    TOOL_DENIED = "tool.denied"
    TOOL_EXECUTED = "tool.executed"
    TOOL_FAILED = "tool.failed"
    EVIDENCE_RECORDED = "evidence.recorded"
    ANALYSIS_VALIDATED = "analysis.validated"
    ANALYSIS_REJECTED = "analysis.rejected"


class AuditEntry(BaseModel):
    """One immutable record of something that happened during a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Monotonically increasing within a run, starting at 1. Gaps mean loss.
    sequence: int = Field(ge=1)
    run_id: RunId
    at: datetime
    event: AuditEventType
    #: The agent id responsible, or "system" for harness-level events.
    actor: str = Field(min_length=1)
    detail: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "audit entry timestamp must be timezone-aware"
            raise ValueError(msg)
        return value
