"""Structured logging with run correlation.

Maturity: hardened.

Why structured, and why correlation ids are bound rather than passed: an
investigation interleaves model calls, authorization decisions, and tool
executions. Prose logs cannot be joined to each other or, from Phase 3, to
OpenTelemetry spans. Binding `run_id`, `incident_id`, and `agent_id` once into
structlog's context variables means every subsequent line in that run carries
them without any call site having to remember.

Logging is diagnostics, not evidence. The audit journal (invariant I7) is the
record of what happened; logs are for debugging how. Nothing in the system may
depend on a log line being present.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TextIO

import structlog

if TYPE_CHECKING:
    from causiq.ids import IncidentId, RunId

_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


def configure_logging(
    *,
    level: str = "INFO",
    log_format: str = "json",
    stream: TextIO | None = None,
) -> None:
    """Configure structlog process-wide.

    `stream` is injectable so tests can assert on real rendered output rather than
    on intercepted event dictionaries - the rendering is part of what we promise.
    """
    if level not in _LEVELS:
        msg = f"unknown log level {level!r}; expected one of {sorted(_LEVELS)}"
        raise ValueError(msg)

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer(sort_keys=True)
        if log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_LEVELS[level]),
        logger_factory=structlog.WriteLoggerFactory(file=stream),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> Any:
    """Return a bound logger for `name`."""
    return structlog.get_logger(name)


def bind_run_context(
    *,
    run_id: RunId,
    incident_id: IncidentId,
    agent_id: str,
) -> None:
    """Bind the correlation ids carried by every log line for the rest of the run."""
    structlog.contextvars.bind_contextvars(
        run_id=str(run_id),
        incident_id=str(incident_id),
        agent_id=agent_id,
    )


def clear_run_context() -> None:
    """Drop the run correlation ids. Always call this when a run ends."""
    structlog.contextvars.clear_contextvars()
