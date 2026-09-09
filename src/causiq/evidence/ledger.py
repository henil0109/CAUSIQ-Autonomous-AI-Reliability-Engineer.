"""The append-only evidence ledger and the citation validator.

Maturity: hardened.

This module is the enforcement point for the two invariants the whole project
rests on. If you read one file to understand why Causiq is trustworthy, read this
one.

**I2 - evidence is immutable and attributable.** `EvidenceLedger` exposes no
update and no delete. Not "we agree not to call them" - they do not exist. The
only way to add a record is `record()`, which mints the id, stamps the acting
identity, and computes the digest itself, so a caller cannot forge provenance.
`verify_integrity()` recomputes every digest to prove nothing was edited after
collection.

**I1 - no claim without evidence.** `validate_citations()` resolves every
evidence id an analysis references against the ids this ledger actually minted.
Anything unresolved makes the analysis invalid, and `require_valid_citations()`
turns that into a `UnresolvedCitationError` that terminates the run. The model
can produce whatever citations it likes; only ones the harness recorded survive.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from causiq.domain.analysis import Analysis
from causiq.domain.enums import EvidenceSource
from causiq.domain.evidence import Evidence, EvidenceRequest
from causiq.errors import LedgerIntegrityError, UnresolvedCitationError
from causiq.ids import EvidenceId, RunId, format_evidence_id


class EvidenceLedger:
    """Append-only store of everything one run observed.

    Scoped to a single run: an id is unique within a ledger, and a record
    belonging to a different run cannot be added.
    """

    def __init__(self, run_id: RunId) -> None:
        self._run_id = run_id
        self._records: list[Evidence] = []
        self._index: dict[EvidenceId, Evidence] = {}

    @property
    def run_id(self) -> RunId:
        return self._run_id

    # -- collection --------------------------------------------------------- #

    def record(
        self,
        *,
        source: EvidenceSource,
        agent_id: str,
        request: EvidenceRequest,
        content: str,
        collected_at: datetime,
        content_type: str = "application/json",
        truncated: bool = False,
    ) -> Evidence:
        """Mint and append one evidence record. The only way evidence is created.

        The id is assigned here, sequentially, so the model cannot choose it and
        the collection order is legible in the audit trail (`ev_001`, `ev_002`, …).
        """
        evidence = Evidence.create(
            evidence_id=format_evidence_id(len(self._records) + 1),
            run_id=self._run_id,
            source=source,
            agent_id=agent_id,
            request=request,
            content=content,
            collected_at=collected_at,
            content_type=content_type,
            truncated=truncated,
        )
        self._append(evidence)
        return evidence

    def rehydrate(self, records: tuple[Evidence, ...]) -> None:
        """Reload a persisted run's evidence, in order.

        Used when reconstructing a run from its record. Rejects anything that
        fails verification, so a tampered journal cannot be quietly reloaded.
        """
        if self._records:
            msg = "cannot rehydrate a ledger that already holds records"
            raise LedgerIntegrityError(msg, run_id=str(self._run_id), held=len(self._records))
        for record in records:
            if not record.verify():
                msg = "evidence failed digest verification during rehydrate"
                raise LedgerIntegrityError(msg, evidence_id=str(record.evidence_id))
            self._append(record)

    def _append(self, evidence: Evidence) -> None:
        """Enforce the append-only invariant. Private on purpose."""
        if evidence.run_id != self._run_id:
            msg = "evidence belongs to a different run"
            raise LedgerIntegrityError(
                msg,
                ledger_run_id=str(self._run_id),
                evidence_run_id=str(evidence.run_id),
                evidence_id=str(evidence.evidence_id),
            )
        if evidence.evidence_id in self._index:
            msg = "evidence id already present in ledger"
            raise LedgerIntegrityError(msg, evidence_id=str(evidence.evidence_id))
        self._records.append(evidence)
        self._index[evidence.evidence_id] = evidence

    # -- reading ------------------------------------------------------------ #

    def get(self, evidence_id: EvidenceId) -> Evidence | None:
        """The record with this id, or None. Never raises for a miss."""
        return self._index.get(evidence_id)

    def snapshot(self) -> tuple[Evidence, ...]:
        """An immutable view, in collection order.

        Returns a tuple rather than the internal list, so a caller cannot append,
        reorder, or remove records through the value we handed them.
        """
        return tuple(self._records)

    def ids(self) -> frozenset[EvidenceId]:
        """Every id this ledger has minted."""
        return frozenset(self._index)

    def __contains__(self, evidence_id: object) -> bool:
        return evidence_id in self._index

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[Evidence]:
        return iter(self._records)

    # -- integrity ---------------------------------------------------------- #

    def verify_integrity(self) -> None:
        """Recompute every digest. Raises `LedgerIntegrityError` on any mismatch.

        Cheap, and the only way "immutable" is a checked property rather than an
        assurance.
        """
        for record in self._records:
            if not record.verify():
                msg = "evidence digest mismatch - record was modified after collection"
                raise LedgerIntegrityError(msg, evidence_id=str(record.evidence_id))


class CitationValidationResult(BaseModel):
    """The outcome of checking an analysis against a ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    #: Every id the analysis referenced.
    cited: frozenset[EvidenceId]
    #: Cited ids with no matching record - the reason an analysis is rejected.
    unresolved: frozenset[EvidenceId]
    #: Collected but never referenced. Informational: high values suggest the
    #: agent gathered evidence it did not use, which the Phase 4 tool-use
    #: evaluator will care about.
    uncited: frozenset[EvidenceId]


def validate_citations(analysis: Analysis, ledger: EvidenceLedger) -> CitationValidationResult:
    """Resolve every citation in `analysis` against `ledger`.

    This is the factual layer of invariant I1. The schema already guarantees each
    claim carries at least one citation; this guarantees those citations point at
    evidence that exists.
    """
    cited = analysis.cited_evidence()
    available = ledger.ids()
    unresolved = cited - available
    return CitationValidationResult(
        valid=not unresolved,
        cited=cited,
        unresolved=unresolved,
        uncited=available - cited,
    )


def require_valid_citations(analysis: Analysis, ledger: EvidenceLedger) -> CitationValidationResult:
    """As `validate_citations`, but raise if any citation is unresolved.

    The run terminates rather than surfacing an analysis whose evidence cannot be
    produced on demand. An unresolvable citation is not a warning to display next
    to a conclusion - it invalidates the conclusion.
    """
    result = validate_citations(analysis, ledger)
    if not result.valid:
        msg = "analysis cites evidence that is not in the run ledger"
        raise UnresolvedCitationError(
            msg,
            run_id=str(ledger.run_id),
            unresolved=sorted(str(item) for item in result.unresolved),
            ledger_size=len(ledger),
        )
    return result
