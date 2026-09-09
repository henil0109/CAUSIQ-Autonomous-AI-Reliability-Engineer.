"""The incident: what Causiq is asked to explain.

Maturity: hardened.

Causiq consumes incidents; it does not detect them (Engineering Contract 1.2).
This model is therefore an input contract - the shape a monitoring system, an
alert, or a human hands over.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from causiq.domain.enums import Severity
from causiq.ids import IncidentId


class Incident(BaseModel):
    """A reported reliability failure awaiting explanation. Immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: IncidentId
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    severity: Severity
    detected_at: datetime
    detected_by: str = Field(min_length=1)

    #: Fully-qualified names of the affected assets, e.g. `analytics.revenue_daily`.
    affected_assets: tuple[str, ...] = ()

    @field_validator("detected_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        """Reject naive timestamps.

        A naive timestamp in an incident record makes the whole correlation
        timeline ambiguous, which is precisely what Causiq exists to get right.
        """
        if value.tzinfo is None:
            msg = "detected_at must be timezone-aware"
            raise ValueError(msg)
        return value
