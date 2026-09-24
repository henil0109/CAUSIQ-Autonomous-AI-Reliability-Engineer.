"""`query_airflow_runs` - the first non-warehouse investigation tool (P1.1).

Maturity: hardened.

Trust boundary, following `causiq.tools.warehouse`'s own stated pattern
exactly (ADR-0009 has the full design basis):

    model
      v
    Tool Registry        (causiq.tools.registry - is this even a known tool?)
      v
    Tool Executor        (causiq.tools.executor  - the security boundary)
      v
    Authorization         (causiq.authz - identity + permission, no approval
                            needed: this tool is read-only)
      v
    query_airflow_runs     <- this module
      v
    AirflowDagIndex        (causiq.artifact_substrate - in-memory only)

**The model is never trusted to turn a string into a filesystem path.**
Unlike `query_warehouse`, this tool has no query language to validate at all:
`AirflowRunsInput.dag_id` is constrained to a narrow identifier pattern at the
schema level (no `/`, `\\`, `.`, or whitespace - see the field's own
description), and `run()` only ever performs a `dict`-backed lookup against
an `AirflowDagIndex` that was built once, at construction time, from a fixed,
application-controlled fixture path
(`causiq.artifact_substrate.DEFAULT_AIRFLOW_FIXTURE_PATH`). There is no code
path in this module, from any input, that opens, joins, or stats a path
derived from `dag_id`.

This module never touches the evidence ledger or the audit journal, exactly
like `query_warehouse`: `run()` returns a `ToolOutput`; the executor is the
only code that turns a successful one into `Evidence` or a failed one into an
audited, evidence-free rejection.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from pydantic import Field

from causiq.artifact_substrate import DEFAULT_AIRFLOW_FIXTURE_PATH, AirflowDagIndex
from causiq.authz import Permission
from causiq.domain.enums import EvidenceSource, RiskLevel
from causiq.errors import ToolExecutionError
from causiq.tools.base import ToolInput, ToolOutput, ToolSpec, canonical_json

#: Airflow dag_ids are conventionally snake_case-ish identifiers. This pattern
#: is deliberately stricter than real Airflow allows (which permits `.` and a
#: few other characters): excluding `/`, `\`, `.`, and whitespace outright
#: means no string that satisfies it could ever be mistaken for, or misused
#: as, a path fragment - defense in depth, even though `run()` never builds a
#: path from this value at all (see the module docstring).
DAG_ID_PATTERN: Final = r"^[A-Za-z0-9_-]{1,200}$"
_DAG_ID_RE: Final = re.compile(DAG_ID_PATTERN)

#: Generous for an in-memory lookup with no I/O; kept for consistency with
#: every other tool declaring a bounded timeout (Engineering Contract 7.2).
DEFAULT_TIMEOUT_SECONDS: Final = 5.0


class AirflowRunsInput(ToolInput):
    """Arguments for `query_airflow_runs`.

    Deliberately a narrow, closed lookup rather than a query language (unlike
    `query_warehouse`'s SQL): Phase 1's artifact sources do not need
    SQL-equivalent expressiveness, and a closed identifier is trivially safe
    to validate (P1 architecture plan §6).
    """

    dag_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=DAG_ID_PATTERN,
        description=(
            "The Airflow DAG id to look up run history for, e.g. "
            "'daily_revenue_pipeline'. Letters, digits, underscore and hyphen only."
        ),
    )


class AirflowDagRunsTool:
    """Read-only lookup of Airflow DAG-run and task-instance history.

    One instance loads the fixture once, at construction, and is safe to
    register once for the life of a process - every call is a pure read
    against the in-memory index built at that time (see `AirflowDagIndex`).
    """

    def __init__(
        self,
        name: str = "query_airflow_runs",
        *,
        fixture_path: Path = DEFAULT_AIRFLOW_FIXTURE_PATH,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._index = AirflowDagIndex.load(fixture_path)
        self._spec = ToolSpec(
            name=name,
            description=(
                "Look up Airflow DAG-run and task-instance history for one DAG. "
                "Returns every recorded run for that DAG, each run's tasks, "
                "their states, and their start/end timestamps. State a clear "
                "reason: what you expect this to show and why."
            ),
            permission=Permission.ARTIFACTS_READ,
            mutating=False,
            risk=RiskLevel.LOW,
            source=EvidenceSource.AIRFLOW,
            timeout_seconds=timeout_seconds,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def input_model(self) -> type[ToolInput]:
        return AirflowRunsInput

    def run(self, payload: ToolInput) -> ToolOutput:
        """Look up `payload.dag_id` in the in-memory index.

        Raises `causiq.errors.ToolExecutionError` for an unknown (but
        well-formed - schema validation already rejected anything else)
        `dag_id`; the executor classifies that as an ordinary tool failure
        (`ToolResultStatus.FAILED`), audits it, and records no evidence -
        exactly the same path a failed `query_warehouse` call takes.
        """
        assert isinstance(payload, AirflowRunsInput)
        dag = self._index.get(payload.dag_id)
        if dag is None:
            known = ", ".join(sorted(self._index.dag_ids())) or "(none)"
            msg = f"no Airflow DAG named {payload.dag_id!r} in the evidence substrate"
            raise ToolExecutionError(msg, dag_id=payload.dag_id, known_dags=known)

        content = canonical_json(dag.model_dump(mode="json"))
        return ToolOutput(content=content, content_type="application/json")


__all__ = ["DAG_ID_PATTERN", "AirflowDagRunsTool", "AirflowRunsInput"]
