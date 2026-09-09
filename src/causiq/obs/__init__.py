"""Observability seam (ADR-0004).

Phase 0 fixes the vocabulary and the interface; Phase 3 supplies the
OpenTelemetry and Langfuse exporters. Nothing here emits telemetry yet, and that
is deliberate: naming the spans now means Phase 3 adds an adapter rather than
editing every call site in the agent loop.
"""

from __future__ import annotations

from causiq.obs.noop import NoOpTracer, RecordedSpan, RecordingTracer
from causiq.obs.ports import Span, SpanName, Tracer

__all__ = [
    "NoOpTracer",
    "RecordedSpan",
    "RecordingTracer",
    "Span",
    "SpanName",
    "Tracer",
]
