"""The root-cause analysis - the artifact the whole system exists to produce.

Maturity: hardened.

Invariant I1 is enforced in three layers, and this module is two of them:

1. The prompt asks for citations. *(a request)*
2. **The schema requires them** - `Claim.citations` has `min_length=1`, so a
   claim with no citation cannot be constructed at all. *(structural)*
3. **The validator proves them** - `causiq.evidence.ledger.validate_citations`
   resolves every cited id against the run's ledger. *(factual)*

Layer 2 stops a well-formed lie about *having* evidence; layer 3 stops a
well-formed lie about *which* evidence. Neither alone is sufficient.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from causiq.domain.enums import AnalysisOutcome, Confidence
from causiq.domain.hypothesis import Hypothesis
from causiq.ids import EvidenceId


class Claim(BaseModel):
    """A single assertion, with the evidence that supports it.

    There is no way to express an uncited claim: `citations` is non-empty by
    schema. This is the structural half of invariant I1.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str = Field(min_length=1)
    citations: tuple[EvidenceId, ...] = Field(min_length=1)


class Analysis(BaseModel):
    """A complete, evidence-backed conclusion about an incident. Immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: AnalysisOutcome
    summary: str = Field(min_length=1)
    #: Present only when the outcome is COMPLETED.
    root_cause: str | None = None
    claims: tuple[Claim, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    confidence: Confidence
    #: What the evidence could not establish. Required when INCONCLUSIVE.
    limitations: str | None = None
    recommended_next_steps: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _outcome_consistency(self) -> Analysis:
        """Keep the two outcomes honest about what they may assert.

        A COMPLETED analysis must name a root cause and support it with at least
        one claim - otherwise "completed" means nothing. An INCONCLUSIVE analysis
        must *not* name a root cause and must say what stopped it, which is what
        makes invariant I8 an honest exit rather than a shrug.
        """
        if self.outcome is AnalysisOutcome.COMPLETED:
            if not self.root_cause:
                msg = "a COMPLETED analysis must state a root_cause"
                raise ValueError(msg)
            if not self.claims:
                msg = "a COMPLETED analysis must make at least one cited claim"
                raise ValueError(msg)
        else:
            if self.root_cause:
                msg = "an INCONCLUSIVE analysis must not state a root_cause"
                raise ValueError(msg)
            if not self.limitations:
                msg = "an INCONCLUSIVE analysis must state its limitations"
                raise ValueError(msg)
        return self

    def cited_evidence(self) -> frozenset[EvidenceId]:
        """Every evidence id referenced anywhere in the analysis.

        Includes hypothesis evidence, so a fabricated id cannot hide in a refuted
        hypothesis where nobody thought to look.
        """
        cited: set[EvidenceId] = set()
        for claim in self.claims:
            cited.update(claim.citations)
        for hypothesis in self.hypotheses:
            cited.update(hypothesis.cited_evidence())
        return frozenset(cited)
