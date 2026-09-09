"""The investigation run record - the persisted artifact of one execution.

Maturity: hardened.

Note the split between this and `EvidenceLedger`. The ledger is a *runtime*
service that enforces append-only collection; this is the *persisted* record that
freezes the result. Keeping them separate means the ledger can enforce its
invariant with real methods, while the run record stays a plain serializable
value that an auditor, a test, or the Phase 4 evaluator can load without
constructing any machinery.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from causiq.domain.analysis import Analysis
from causiq.domain.budget import Budget, BudgetUsage
from causiq.domain.enums import TERMINAL_RUN_STATES, RunState
from causiq.domain.evidence import Evidence
from causiq.ids import IncidentId, RunId


class InvestigationRun(BaseModel):
    """One bounded execution against one incident. Immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    incident_id: IncidentId
    agent_id: str = Field(min_length=1)
    state: RunState
    budget: Budget
    usage: BudgetUsage = BudgetUsage()
    started_at: datetime
    ended_at: datetime | None = None
    evidence: tuple[Evidence, ...] = ()
    analysis: Analysis | None = None
    #: Populated for FAILED / DENIED / BUDGET_EXCEEDED runs.
    failure_reason: str | None = None

    @field_validator("started_at", "ended_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            msg = "run timestamps must be timezone-aware"
            raise ValueError(msg)
        return value

    @property
    def is_terminal(self) -> bool:
        """Whether this run has reached a state it will never leave."""
        return self.state in TERMINAL_RUN_STATES

    @model_validator(mode="after")
    def _terminal_states_are_complete(self) -> InvestigationRun:
        """A terminal run must be a complete record.

        Enforces the second half of invariant I8: every run ends in a *recorded*
        terminal state. A COMPLETED run without an analysis, or a terminal run
        without an end time, is a record that cannot be audited.
        """
        if self.is_terminal:
            if self.ended_at is None:
                msg = f"a terminal run ({self.state}) must have ended_at set"
                raise ValueError(msg)
            if self.ended_at < self.started_at:
                msg = "ended_at must not precede started_at"
                raise ValueError(msg)
        elif self.ended_at is not None:
            msg = f"a non-terminal run ({self.state}) must not have ended_at set"
            raise ValueError(msg)

        if self.state is RunState.COMPLETED and self.analysis is None:
            msg = "a COMPLETED run must carry its analysis"
            raise ValueError(msg)
        return self

    def evidence_ids(self) -> frozenset[str]:
        """Ids of every evidence record captured by this run."""
        return frozenset(str(record.evidence_id) for record in self.evidence)
