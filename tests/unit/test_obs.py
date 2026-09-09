"""The observability seam (ADR-0004).

Nothing here exports telemetry - Phase 3 does that. These tests protect the two
things Phase 0 is responsible for: the span vocabulary, and the fact that both
tracer implementations behave identically at the call site.
"""

from __future__ import annotations

import pytest

from causiq.obs import NoOpTracer, RecordingTracer, Span, SpanName, Tracer

pytestmark = pytest.mark.unit


def test_both_tracers_satisfy_the_port() -> None:
    assert isinstance(NoOpTracer(), Tracer)
    assert isinstance(RecordingTracer(), Tracer)


def test_span_vocabulary_is_the_documented_one() -> None:
    """Engineering Contract 9. Renaming a span breaks Phase 3 dashboards and
    Phase 4 eval queries, so the vocabulary is asserted, not assumed."""
    assert {span.value for span in SpanName} == {
        "causiq.run",
        "causiq.agent.turn",
        "causiq.model.call",
        "causiq.tool.authorize",
        "causiq.tool.execute",
        "causiq.evidence.record",
        "causiq.analysis.validate",
    }


def test_noop_tracer_accepts_and_discards() -> None:
    tracer = NoOpTracer()
    with tracer.span(SpanName.RUN, run_id="run_0001") as span:
        assert isinstance(span, Span)
        span.set_attribute("evidence_count", 2)


def test_recording_tracer_captures_names_in_order() -> None:
    tracer = RecordingTracer()
    with tracer.span(SpanName.RUN):
        with tracer.span(SpanName.AGENT_TURN, turn=1):
            pass
        with tracer.span(SpanName.TOOL_AUTHORIZE, tool="query_warehouse"):
            pass
    assert tracer.names() == (SpanName.RUN, SpanName.AGENT_TURN, SpanName.TOOL_AUTHORIZE)


def test_recording_tracer_captures_attributes() -> None:
    tracer = RecordingTracer()
    with tracer.span(SpanName.MODEL_CALL, model="claude-opus-5") as span:
        span.set_attribute("input_tokens", 1234)
        span.set_attribute("cached", True)
    (recorded,) = tracer.spans
    assert recorded.attributes == {
        "model": "claude-opus-5",
        "input_tokens": 1234,
        "cached": True,
    }
    assert recorded.closed


def test_spans_close_even_when_the_body_raises() -> None:
    tracer = RecordingTracer()
    with pytest.raises(RuntimeError), tracer.span(SpanName.TOOL_EXECUTE):
        msg = "tool blew up"
        raise RuntimeError(msg)
    assert tracer.spans[0].closed
