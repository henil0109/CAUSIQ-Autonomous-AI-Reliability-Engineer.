"""Phase 0 tracer implementations.

`NoOpTracer` is what runs in Phase 0: the call sites exist, the spans are named,
and nothing is exported. `RecordingTracer` is its test counterpart, so the tests
that will matter in Phase 3 - "does the agent loop open the spans it claims to" -
can be written against the seam today.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from causiq.obs.ports import AttributeValue, SpanName


class _NoOpSpan:
    """Accepts attributes and discards them."""

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        """No-op. Present so call sites are identical under every tracer."""


class NoOpTracer:
    """The Phase 0 tracer. Zero overhead, zero output."""

    @contextmanager
    def span(self, name: SpanName, **attributes: AttributeValue) -> Iterator[_NoOpSpan]:
        del name, attributes
        yield _NoOpSpan()


@dataclass
class RecordedSpan:
    """A span captured by `RecordingTracer`."""

    name: SpanName
    attributes: dict[str, AttributeValue] = field(default_factory=dict)
    closed: bool = False

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        self.attributes[key] = value


class RecordingTracer:
    """Test implementation: keeps every span opened, in order."""

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []

    @contextmanager
    def span(self, name: SpanName, **attributes: AttributeValue) -> Iterator[RecordedSpan]:
        recorded = RecordedSpan(name=name, attributes=dict(attributes))
        self.spans.append(recorded)
        try:
            yield recorded
        finally:
            recorded.closed = True

    def names(self) -> tuple[SpanName, ...]:
        """Span names in the order they were opened."""
        return tuple(span.name for span in self.spans)
