"""`query_dbt_run_results` - the second non-warehouse investigation tool
(P1.2).

Maturity: hardened.

Trust boundary, following `causiq.tools.airflow`'s own stated pattern exactly
(ADR-0009 has the full design basis, now amended to cover this second
source):

    model
      v
    Tool Registry        (causiq.tools.registry - is this even a known tool?)
      v
    Tool Executor        (causiq.tools.executor  - the security boundary)
      v
    Authorization         (causiq.authz - identity + permission, no approval
                            needed: this tool is read-only)
      v
    query_dbt_run_results  <- this module
      v
    DbtModelIndex          (causiq.dbt_substrate - in-memory only)

**The model is never trusted to turn a string into a filesystem path.**
`DbtRunResultsInput.unique_id` is constrained to dbt's real identifier
grammar at the schema level (letters/digits/underscore segments joined by
single dots - no `/`, `\\`, `..`, or whitespace anywhere), and `run()` only
ever performs a `dict`-backed lookup against a `DbtModelIndex` that was built
once, at construction time, from a fixed, application-controlled fixture
path (`causiq.dbt_substrate.DEFAULT_DBT_FIXTURE_PATH`). There is no code path
in this module, from any input, that opens, joins, or stats a path derived
from `unique_id`.

This module never touches the evidence ledger or the audit journal, exactly
like `query_airflow_runs`: `run()` returns a `ToolOutput`; the executor is
the only code that turns a successful one into `Evidence` or a failed one
into an audited, evidence-free rejection.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from pydantic import Field

from causiq.authz import Permission
from causiq.dbt_substrate import DEFAULT_DBT_FIXTURE_PATH, DbtModelIndex
from causiq.domain.enums import EvidenceSource, RiskLevel
from causiq.errors import ToolExecutionError
from causiq.tools.base import ToolInput, ToolOutput, ToolSpec, canonical_json

#: dbt's real `unique_id` grammar is `<resource_type>.<package>.<name>`
#: (dots as segment separators). This pattern permits exactly that shape -
#: one or more alphanumeric/underscore segments joined by single dots - and
#: cannot produce `..`, a leading/trailing dot, or any path separator, so it
#: stays as path-injection-safe as `causiq.tools.airflow.DAG_ID_PATTERN`
#: while matching dbt's real identifier grammar instead of Airflow's.
UNIQUE_ID_PATTERN: Final = r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$"
_UNIQUE_ID_RE: Final = re.compile(UNIQUE_ID_PATTERN)

#: Generous for an in-memory lookup with no I/O; matches
#: `causiq.tools.airflow.DEFAULT_TIMEOUT_SECONDS`.
DEFAULT_TIMEOUT_SECONDS: Final = 5.0


class DbtRunResultsInput(ToolInput):
    """Arguments for `query_dbt_run_results`.

    Deliberately a narrow, closed lookup, exactly like
    `causiq.tools.airflow.AirflowRunsInput` - one bounded identifier, no
    query language, no date-range or run-id filter (P1 architecture plan §6).
    """

    unique_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=UNIQUE_ID_PATTERN,
        description=(
            "The dbt model's unique_id, e.g. "
            "'model.revenue_analytics.daily_revenue_pipeline'. "
            "Letters, digits, and underscore segments joined by single dots only."
        ),
    )


class DbtRunResultsTool:
    """Read-only lookup of dbt model run-result history.

    One instance loads the fixture once, at construction, and is safe to
    register once for the life of a process - every call is a pure read
    against the in-memory index built at that time (see `DbtModelIndex`).
    """

    def __init__(
        self,
        name: str = "query_dbt_run_results",
        *,
        fixture_path: Path = DEFAULT_DBT_FIXTURE_PATH,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._index = DbtModelIndex.load(fixture_path)
        self._spec = ToolSpec(
            name=name,
            description=(
                "Look up dbt model run-result history for one model. Returns "
                "every recorded invocation for that model, its status "
                "(success, error, or skipped), and its execution time. State "
                "a clear reason: what you expect this to show and why."
            ),
            permission=Permission.ARTIFACTS_READ,
            mutating=False,
            risk=RiskLevel.LOW,
            source=EvidenceSource.DBT,
            timeout_seconds=timeout_seconds,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def input_model(self) -> type[ToolInput]:
        return DbtRunResultsInput

    def run(self, payload: ToolInput) -> ToolOutput:
        """Look up `payload.unique_id` in the in-memory index.

        Raises `causiq.errors.ToolExecutionError` for an unknown (but
        well-formed - schema validation already rejected anything else)
        `unique_id`; the executor classifies that as an ordinary tool
        failure (`ToolResultStatus.FAILED`), audits it, and records no
        evidence - exactly the same path a failed `query_airflow_runs` call
        takes.
        """
        assert isinstance(payload, DbtRunResultsInput)
        model = self._index.get(payload.unique_id)
        if model is None:
            known = ", ".join(sorted(self._index.unique_ids())) or "(none)"
            msg = f"no dbt model named {payload.unique_id!r} in the evidence substrate"
            raise ToolExecutionError(msg, unique_id=payload.unique_id, known_models=known)

        content = canonical_json(model.model_dump(mode="json"))
        return ToolOutput(content=content, content_type="application/json")


__all__ = ["UNIQUE_ID_PATTERN", "DbtRunResultsInput", "DbtRunResultsTool"]
