"""The Causiq domain model - pure data, zero I/O.

Maturity: hardened.

Nothing in this package imports a database driver, an HTTP client, or the
Anthropic SDK. That is what allows the domain to be validated, serialized,
diffed, and tested with no infrastructure, and it is the reason the layering rule
in Engineering Contract 4.1 points only downward.
"""

from __future__ import annotations

from causiq.domain.analysis import Analysis, Claim
from causiq.domain.audit import AuditEntry, AuditEventType
from causiq.domain.budget import Budget, BudgetTracker, BudgetUsage
from causiq.domain.enums import (
    TERMINAL_RUN_STATES,
    AgentRole,
    AnalysisOutcome,
    Confidence,
    EvidenceSource,
    HypothesisStatus,
    RunState,
    Severity,
)
from causiq.domain.evidence import Evidence, EvidenceRequest
from causiq.domain.hypothesis import Hypothesis
from causiq.domain.incident import Incident
from causiq.domain.run import InvestigationRun

__all__ = [
    "TERMINAL_RUN_STATES",
    "AgentRole",
    "Analysis",
    "AnalysisOutcome",
    "AuditEntry",
    "AuditEventType",
    "Budget",
    "BudgetTracker",
    "BudgetUsage",
    "Claim",
    "Confidence",
    "Evidence",
    "EvidenceRequest",
    "EvidenceSource",
    "Hypothesis",
    "HypothesisStatus",
    "Incident",
    "InvestigationRun",
    "RunState",
    "Severity",
]
