"""Evidence: an immutable, attributable fact retrieved by an authorized tool.

Maturity: hardened.

This is the load-bearing model of the whole system. Invariant I2 says evidence
records what was asked, what came back, which tool answered, under which identity,
and when. Invariant I1 says an analysis may only cite ids that exist here.

The `digest` is what makes "immutable" checkable rather than merely intended. It
is computed over the *attesting* fields - not over `evidence_id` (assigned by the
ledger, so it would couple the digest to collection order) and not over `digest`
itself. Recomputing it later proves the record was not edited after the fact.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from causiq.domain.enums import EvidenceSource
from causiq.ids import EvidenceId, RunId


def _canonical_json(payload: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace.

    Determinism matters twice over - the digest must be reproducible, and
    invariant I6 requires byte-identical audit journals across runs.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


class EvidenceRequest(BaseModel):
    """Exactly what was asked of the tool, preserved verbatim.

    `reason` is required rather than optional. Forcing the caller to state why it
    is asking before it asks puts the intent in the audit trail and makes tool use
    gradeable by the Phase 4 evaluator - one schema field for a real
    explainability signal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    reason: str = Field(min_length=1)


class Evidence(BaseModel):
    """One recorded fact. Immutable and self-attesting."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: EvidenceId
    run_id: RunId
    source: EvidenceSource
    #: The identity that collected it - attribution half of invariant I2.
    agent_id: str = Field(min_length=1)
    request: EvidenceRequest
    #: Serialized result content. Opaque here; the tool decides its encoding.
    content: str
    content_type: str = "application/json"
    collected_at: datetime
    #: True when the tool capped the result (row or byte limit).
    truncated: bool = False
    #: SHA-256 over the attesting fields. See `compute_digest`.
    digest: str = Field(min_length=64, max_length=64)

    @field_validator("collected_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "collected_at must be timezone-aware"
            raise ValueError(msg)
        return value

    @staticmethod
    def compute_digest(
        *,
        run_id: RunId,
        source: EvidenceSource,
        agent_id: str,
        request: EvidenceRequest,
        content: str,
        content_type: str,
        collected_at: datetime,
        truncated: bool,
    ) -> str:
        """SHA-256 over everything the record attests to."""
        payload = {
            "run_id": str(run_id),
            "source": str(source),
            "agent_id": agent_id,
            "request": request.model_dump(mode="json"),
            "content": content,
            "content_type": content_type,
            "collected_at": collected_at.isoformat(),
            "truncated": truncated,
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        evidence_id: EvidenceId,
        run_id: RunId,
        source: EvidenceSource,
        agent_id: str,
        request: EvidenceRequest,
        content: str,
        collected_at: datetime,
        content_type: str = "application/json",
        truncated: bool = False,
    ) -> Self:
        """Build a record with its digest computed rather than supplied.

        Callers cannot hand in a digest, so a record with a wrong digest can only
        arise from tampering - which `verify()` then detects.
        """
        digest = cls.compute_digest(
            run_id=run_id,
            source=source,
            agent_id=agent_id,
            request=request,
            content=content,
            content_type=content_type,
            collected_at=collected_at,
            truncated=truncated,
        )
        return cls(
            evidence_id=evidence_id,
            run_id=run_id,
            source=source,
            agent_id=agent_id,
            request=request,
            content=content,
            content_type=content_type,
            collected_at=collected_at,
            truncated=truncated,
            digest=digest,
        )

    def verify(self) -> bool:
        """Whether the stored digest still matches the record's content."""
        return self.digest == self.compute_digest(
            run_id=self.run_id,
            source=self.source,
            agent_id=self.agent_id,
            request=self.request,
            content=self.content,
            content_type=self.content_type,
            collected_at=self.collected_at,
            truncated=self.truncated,
        )
