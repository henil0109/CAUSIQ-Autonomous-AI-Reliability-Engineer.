"""Candidate explanations and their standing against the evidence.

Maturity: hardened.

A hypothesis is how Causiq shows its working. Recording refuted hypotheses is as
important as recording the surviving one: it is the difference between an
investigation and an assertion, and in review it is the artifact that proves the
agent considered and eliminated alternatives.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from causiq.domain.enums import HypothesisStatus
from causiq.ids import EvidenceId, HypothesisId


class Hypothesis(BaseModel):
    """A candidate cause, with the evidence for and against it. Immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_id: HypothesisId
    statement: str = Field(min_length=1)
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    supporting_evidence: tuple[EvidenceId, ...] = ()
    refuting_evidence: tuple[EvidenceId, ...] = ()

    @model_validator(mode="after")
    def _status_requires_evidence(self) -> Hypothesis:
        """A verdict must be earned.

        Marking a hypothesis supported or refuted without citing anything is
        exactly the failure mode invariant I1 exists to prevent, so it is blocked
        at the schema level rather than left to the citation validator.
        """
        if self.status is HypothesisStatus.SUPPORTED and not self.supporting_evidence:
            msg = "a SUPPORTED hypothesis must cite at least one supporting evidence id"
            raise ValueError(msg)
        if self.status is HypothesisStatus.REFUTED and not self.refuting_evidence:
            msg = "a REFUTED hypothesis must cite at least one refuting evidence id"
            raise ValueError(msg)
        return self

    def cited_evidence(self) -> frozenset[EvidenceId]:
        """Every evidence id this hypothesis references, for or against."""
        return frozenset(self.supporting_evidence) | frozenset(self.refuting_evidence)
