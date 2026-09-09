"""Evidence collection and citation validation - invariants I1 and I2.

Maturity: hardened.
"""

from __future__ import annotations

from causiq.evidence.ledger import (
    CitationValidationResult,
    EvidenceLedger,
    require_valid_citations,
    validate_citations,
)

__all__ = [
    "CitationValidationResult",
    "EvidenceLedger",
    "require_valid_citations",
    "validate_citations",
]
