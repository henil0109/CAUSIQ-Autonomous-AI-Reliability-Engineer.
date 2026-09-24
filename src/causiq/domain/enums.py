"""Closed vocabularies used across the domain.

Maturity: hardened.

These are `StrEnum` so they serialize to readable strings in audit journals and
JSON Schema, which matters because those artifacts are read by humans in review
and by the Phase 4 evaluator.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Severity(StrEnum):
    """Incident severity, as reported by whatever detected it."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RunState(StrEnum):
    """Lifecycle of an investigation run.

    Every run ends in one of `TERMINAL_RUN_STATES` (invariant I8) - including the
    unhappy ones, which are outcomes rather than exceptions.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    INCONCLUSIVE = "inconclusive"
    DENIED = "denied"
    BUDGET_EXCEEDED = "budget_exceeded"
    FAILED = "failed"


#: States from which a run never transitions again.
TERMINAL_RUN_STATES: Final[frozenset[RunState]] = frozenset(
    {
        RunState.COMPLETED,
        RunState.INCONCLUSIVE,
        RunState.DENIED,
        RunState.BUDGET_EXCEEDED,
        RunState.FAILED,
    }
)


class AnalysisOutcome(StrEnum):
    """Whether the investigation reached a supported conclusion.

    `INCONCLUSIVE` is a first-class result, not a failure (invariant I8). An agent
    that cannot support a conclusion from the evidence it gathered must say so.
    """

    COMPLETED = "completed"
    INCONCLUSIVE = "inconclusive"


class Confidence(StrEnum):
    """How strongly the evidence supports the stated conclusion."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceSource(StrEnum):
    """Systems Causiq may collect evidence from.

    Phase 0 had exactly one. Phase 1 adds DBT, GIT, DEPLOYMENT and DATA_QUALITY
    as their tools land - deliberately not declared before then, so the enum
    never describes a capability that does not exist. AIRFLOW is the first of
    those, added in P1.1 alongside `causiq.tools.airflow.AirflowDagRunsTool`
    (ADR-0009).
    """

    WAREHOUSE = "warehouse"
    AIRFLOW = "airflow"


class HypothesisStatus(StrEnum):
    """Where a candidate explanation stands against the evidence."""

    PROPOSED = "proposed"
    SUPPORTED = "supported"
    REFUTED = "refuted"


class RiskLevel(StrEnum):
    """How much damage a tool could do if it misbehaved.

    Distinct from `mutating`, which is a hard security classification. Risk is
    advisory metadata: a read-only tool against production can still be HIGH if
    it is expensive or contention-prone. Phase 5 uses it to decide which
    remediations need a stricter approval; Phase 0 records it so the audit trail
    carries it from the start.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AgentRole(StrEnum):
    """Roles an agent identity may hold.

    Phase 2 adds the specialist roles when sub-agent decomposition lands.
    """

    INVESTIGATOR = "investigator"
