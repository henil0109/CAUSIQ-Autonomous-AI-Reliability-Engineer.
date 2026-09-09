"""Citation validation - invariant I1, "no claim without evidence".

This is the most important test module in Phase 0. AC-6 lives here:
`test_fabricated_citation_is_rejected`.
"""

from __future__ import annotations

import pytest

from causiq.domain import (
    Analysis,
    AnalysisOutcome,
    Claim,
    Confidence,
    Hypothesis,
    HypothesisStatus,
)
from causiq.errors import UnresolvedCitationError
from causiq.evidence import EvidenceLedger, require_valid_citations, validate_citations
from causiq.ids import EvidenceId, HypothesisId
from tests.conftest import make_analysis

pytestmark = pytest.mark.unit


def test_valid_citations_pass(ledger: EvidenceLedger) -> None:
    analysis = make_analysis(citations=("ev_001", "ev_002"))
    result = validate_citations(analysis, ledger)
    assert result.valid
    assert result.unresolved == frozenset()
    assert result.cited == {EvidenceId("ev_001"), EvidenceId("ev_002")}
    assert result.uncited == frozenset()
    assert require_valid_citations(analysis, ledger).valid


def test_fabricated_citation_is_rejected(ledger: EvidenceLedger) -> None:
    """AC-6. An analysis citing evidence that was never recorded is rejected.

    This is the single behaviour that separates Causiq from a model that talks
    confidently about failures. The ledger holds ev_001 and ev_002; the analysis
    cites ev_042, which no tool ever produced.
    """
    analysis = make_analysis(citations=("ev_001", "ev_042"))

    result = validate_citations(analysis, ledger)
    assert not result.valid
    assert result.unresolved == {EvidenceId("ev_042")}

    with pytest.raises(UnresolvedCitationError) as caught:
        require_valid_citations(analysis, ledger)
    assert caught.value.context["unresolved"] == ["ev_042"]
    assert caught.value.context["ledger_size"] == 2
    # Fatal to the run, by classification.
    assert not caught.value.recoverable


def test_every_fabricated_citation_is_reported(ledger: EvidenceLedger) -> None:
    """A reviewer needs the full list, not the first failure."""
    analysis = make_analysis(citations=("ev_042", "ev_043", "ev_001"))
    result = validate_citations(analysis, ledger)
    assert result.unresolved == {EvidenceId("ev_042"), EvidenceId("ev_043")}


def test_fabricated_citation_hidden_in_a_hypothesis_is_caught(ledger: EvidenceLedger) -> None:
    """Hypothesis evidence is validated too.

    Without this, a fabricated id could hide inside a refuted hypothesis - the
    part of the output nobody scrutinises.
    """
    analysis = Analysis(
        outcome=AnalysisOutcome.COMPLETED,
        summary="s",
        root_cause="rc",
        claims=(Claim(statement="c", citations=(EvidenceId("ev_001"),)),),
        hypotheses=(
            Hypothesis(
                hypothesis_id=HypothesisId("hyp_001"),
                statement="Upstream dropped rows",
                status=HypothesisStatus.REFUTED,
                refuting_evidence=(EvidenceId("ev_999"),),
            ),
        ),
        confidence=Confidence.MEDIUM,
    )
    result = validate_citations(analysis, ledger)
    assert not result.valid
    assert result.unresolved == {EvidenceId("ev_999")}


def test_uncited_evidence_is_reported_but_not_fatal(ledger: EvidenceLedger) -> None:
    """Collecting evidence and not using it is a signal, not an error.

    The Phase 4 tool-use evaluator will care; the run should not fail.
    """
    analysis = make_analysis(citations=("ev_001",))
    result = validate_citations(analysis, ledger)
    assert result.valid
    assert result.uncited == {EvidenceId("ev_002")}


def test_inconclusive_analysis_still_has_citations_validated(ledger: EvidenceLedger) -> None:
    """Invariant I8 is an honest exit, not an exemption from I1."""
    analysis = Analysis(
        outcome=AnalysisOutcome.INCONCLUSIVE,
        summary="Could not establish a cause.",
        limitations="No access to the dbt model definition in Phase 0.",
        claims=(Claim(statement="Volume was unchanged", citations=(EvidenceId("ev_404"),)),),
        confidence=Confidence.LOW,
    )
    with pytest.raises(UnresolvedCitationError):
        require_valid_citations(analysis, ledger)


def test_analysis_with_no_claims_cites_nothing(ledger: EvidenceLedger) -> None:
    analysis = Analysis(
        outcome=AnalysisOutcome.INCONCLUSIVE,
        summary="Nothing conclusive.",
        limitations="Evidence surface is limited to the warehouse in Phase 0.",
        confidence=Confidence.LOW,
    )
    result = validate_citations(analysis, ledger)
    assert result.valid
    assert result.cited == frozenset()
    assert result.uncited == ledger.ids()
