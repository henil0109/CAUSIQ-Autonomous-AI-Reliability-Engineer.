"""Prefixed identifiers and the id-generation port.

Maturity: hardened.

Two decisions worth explaining in review:

**Ids are prefixed and human-readable.** `run_…`, `inc_…`, `ev_001`. When a
reviewer follows a citation from an analysis back to the query that produced it,
they read `ev_003`, not a UUID. Legibility is a feature of the audit trail.

**Evidence ids are sequential and minted only by the ledger.** They are unique
within a run, assigned in the order evidence was collected, and there is no other
code path that can create one. That is a small but real reinforcement of invariant
I1: the model cannot mint an evidence id, only cite one, and a citation that was
never minted fails validation.

`IdGenerator` is a port (ADR-0004) for the same reason `Clock` is: without it,
run ids are random and the byte-identical-audit-journal guarantee (I6, AC-10) is
impossible to assert.
"""

from __future__ import annotations

import uuid
from typing import Final, NewType, Protocol, runtime_checkable

RunId = NewType("RunId", str)
IncidentId = NewType("IncidentId", str)
EvidenceId = NewType("EvidenceId", str)
HypothesisId = NewType("HypothesisId", str)
ApprovalId = NewType("ApprovalId", str)

RUN_PREFIX: Final = "run_"
INCIDENT_PREFIX: Final = "inc_"
EVIDENCE_PREFIX: Final = "ev_"
HYPOTHESIS_PREFIX: Final = "hyp_"
APPROVAL_PREFIX: Final = "apr_"

#: Width of the sequential portion of an evidence id, e.g. `ev_001`.
EVIDENCE_SEQUENCE_WIDTH: Final = 3


def format_evidence_id(sequence: int) -> EvidenceId:
    """Build the evidence id for the n-th record in a ledger (1-based).

    Zero-padded so ids sort lexicographically in the order they were collected,
    which keeps audit journals readable without a sort key.
    """
    if sequence < 1:
        msg = f"evidence sequence must be >= 1, got {sequence}"
        raise ValueError(msg)
    return EvidenceId(f"{EVIDENCE_PREFIX}{sequence:0{EVIDENCE_SEQUENCE_WIDTH}d}")


def format_hypothesis_id(sequence: int) -> HypothesisId:
    """Build the hypothesis id for the n-th hypothesis in a run (1-based)."""
    if sequence < 1:
        msg = f"hypothesis sequence must be >= 1, got {sequence}"
        raise ValueError(msg)
    return HypothesisId(f"{HYPOTHESIS_PREFIX}{sequence:0{EVIDENCE_SEQUENCE_WIDTH}d}")


def is_evidence_id(value: str) -> bool:
    """Whether `value` is shaped like an evidence id.

    A shape check only. Resolution against a ledger is what actually matters and
    is done by `causiq.evidence.ledger.validate_citations`.
    """
    suffix = value.removeprefix(EVIDENCE_PREFIX)
    return value.startswith(EVIDENCE_PREFIX) and suffix.isdigit() and len(suffix) >= 1


@runtime_checkable
class IdGenerator(Protocol):
    """Port: mints identifiers that are not derived from a sequence."""

    def new_run_id(self) -> RunId: ...


class Uuid4IdGenerator:
    """Production implementation: random, collision-free across machines."""

    def new_run_id(self) -> RunId:
        return RunId(f"{RUN_PREFIX}{uuid.uuid4().hex[:16]}")


class FixedIdGenerator:
    """Deterministic implementation for tests.

    Yields `run_0001`, `run_0002`, ... so a whole investigation - including its
    audit journal - is byte-reproducible when paired with `FrozenClock`.
    """

    def __init__(self, *, prefix: str = RUN_PREFIX, start: int = 1) -> None:
        self._prefix = prefix
        self._next = start

    def new_run_id(self) -> RunId:
        value = RunId(f"{self._prefix}{self._next:04d}")
        self._next += 1
        return value
