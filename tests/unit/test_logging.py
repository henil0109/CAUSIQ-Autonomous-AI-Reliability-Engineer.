"""Structured logging: real rendered output, not intercepted event dicts.

The tests assert on what actually reaches the stream, because the rendering - JSON
with correlation ids - is the part other systems depend on.
"""

from __future__ import annotations

import io
import json

import pytest

from causiq.logging import bind_run_context, clear_run_context, configure_logging, get_logger
from tests.conftest import INCIDENT_ID, RUN_ID

pytestmark = pytest.mark.unit


@pytest.fixture
def stream() -> io.StringIO:
    return io.StringIO()


@pytest.fixture(autouse=True)
def _clear_context() -> object:
    clear_run_context()
    yield
    clear_run_context()


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_emits_parseable_json(stream: io.StringIO) -> None:
    configure_logging(level="INFO", log_format="json", stream=stream)
    get_logger("test").info("evidence_recorded", evidence_id="ev_001")
    (record,) = _lines(stream)
    assert record["event"] == "evidence_recorded"
    assert record["evidence_id"] == "ev_001"
    assert record["level"] == "info"
    assert "timestamp" in record


def test_run_context_is_bound_to_every_line(stream: io.StringIO) -> None:
    """Correlation ids come from context, so no call site has to remember them."""
    configure_logging(level="INFO", log_format="json", stream=stream)
    bind_run_context(run_id=RUN_ID, incident_id=INCIDENT_ID, agent_id="agent_investigator_0")
    log = get_logger("test")
    log.info("first")
    log.info("second")

    records = _lines(stream)
    assert len(records) == 2
    for record in records:
        assert record["run_id"] == "run_0001"
        assert record["incident_id"] == "inc_INC-001"
        assert record["agent_id"] == "agent_investigator_0"


def test_context_is_cleared_at_run_end(stream: io.StringIO) -> None:
    configure_logging(level="INFO", log_format="json", stream=stream)
    bind_run_context(run_id=RUN_ID, incident_id=INCIDENT_ID, agent_id="agent_a")
    clear_run_context()
    get_logger("test").info("after_run")
    (record,) = _lines(stream)
    assert "run_id" not in record


def test_level_filtering(stream: io.StringIO) -> None:
    configure_logging(level="WARNING", log_format="json", stream=stream)
    log = get_logger("test")
    log.info("suppressed")
    log.warning("kept")
    records = _lines(stream)
    assert [record["event"] for record in records] == ["kept"]


def test_console_format_is_available_for_humans(stream: io.StringIO) -> None:
    configure_logging(level="INFO", log_format="console", stream=stream)
    get_logger("test").info("human_readable", evidence_id="ev_001")
    output = stream.getvalue()
    assert "human_readable" in output
    assert "ev_001" in output


def test_unknown_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown log level"):
        configure_logging(level="LOUD")


def test_exception_info_is_rendered(stream: io.StringIO) -> None:
    configure_logging(level="INFO", log_format="json", stream=stream)
    try:
        msg = "boom"
        raise RuntimeError(msg)
    except RuntimeError:
        get_logger("test").exception("tool_failed")
    (record,) = _lines(stream)
    assert record["event"] == "tool_failed"
    assert "RuntimeError" in str(record.get("exception", ""))
