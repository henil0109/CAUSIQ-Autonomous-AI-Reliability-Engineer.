"""The Tracer port and the fixed span vocabulary.

Maturity: hardened (as a seam; it emits nothing until Phase 3).

The span names below are the contract between Phase 0 and Phase 3. They are an
enum rather than free strings so that a typo is a type error, and so that the
Phase 4 evaluator can key on them without a magic-string table.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from enum import StrEnum
from typing import Protocol, runtime_checkable

#: Values a span attribute may carry. Deliberately narrow - attributes are
#: telemetry, not a place to smuggle objects.
AttributeValue = str | int | float | bool


class SpanName(StrEnum):
    """The complete span vocabulary (Engineering Contract 9).

    Fixed in Phase 0 even though nothing emits them yet. Adding a name later is
    fine; renaming one is a breaking change to dashboards and eval queries.
    """

    RUN = "causiq.run"
    AGENT_TURN = "causiq.agent.turn"
    MODEL_CALL = "causiq.model.call"
    TOOL_AUTHORIZE = "causiq.tool.authorize"
    TOOL_EXECUTE = "causiq.tool.execute"
    EVIDENCE_RECORD = "causiq.evidence.record"
    ANALYSIS_VALIDATE = "causiq.analysis.validate"


@runtime_checkable
class Span(Protocol):
    """An in-progress unit of work."""

    def set_attribute(self, key: str, value: AttributeValue) -> None: ...


@runtime_checkable
class Tracer(Protocol):
    """Port: opens spans. Phase 3 supplies an OpenTelemetry-backed implementation."""

    def span(
        self,
        name: SpanName,
        **attributes: AttributeValue,
    ) -> AbstractContextManager[Span]: ...
