"""`query_dq_check_results` - the third non-warehouse investigation tool
(P1.3).

Maturity: hardened.

Trust boundary, following `causiq.tools.airflow`/`causiq.tools.dbt`'s own
stated pattern exactly (ADR-0009 has the full design basis, now amended to
cover this third source):

    model
      v
    Tool Registry          (causiq.tools.registry - is this even a known tool?)
      v
    Tool Executor          (causiq.tools.executor  - the security boundary)
      v
    Authorization           (causiq.authz - identity + permission, no approval
                             needed: this tool is read-only)
      v
    query_dq_check_results  <- this module
      v
    DqCheckIndex            (causiq.dq_substrate - in-memory only)

**The model is never trusted to turn a string into a filesystem path.**
`DqCheckResultsInput.check_name` is constrained to a narrow identifier
pattern at the schema level (no `/`, `\\`, `.`, or whitespace - the same
shape `causiq.tools.airflow.DAG_ID_PATTERN` uses, since DQ check names are
conventionally simple identifiers rather than dbt's dotted grammar), and
`run()` only ever performs a `dict`-backed lookup against a `DqCheckIndex`
that was built once, at construction time, from a fixed, application-
controlled fixture path (`causiq.dq_substrate.DEFAULT_DQ_FIXTURE_PATH`).
There is no code path in this module, from any input, that opens, joins, or
stats a path derived from `check_name`.

This module never touches the evidence ledger or the audit journal, exactly
like `query_airflow_runs`/`query_dbt_run_results`: `run()` returns a
`ToolOutput`; the executor is the only code that turns a successful one into
`Evidence` or a failed one into an audited, evidence-free rejection.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from pydantic import Field

from causiq.authz import Permission
from causiq.domain.enums import EvidenceSource, RiskLevel
from causiq.dq_substrate import DEFAULT_DQ_FIXTURE_PATH, DqCheckIndex
from causiq.errors import ToolExecutionError
from causiq.tools.base import ToolInput, ToolOutput, ToolSpec, canonical_json

#: DQ check names are conventionally simple identifiers - the same shape
#: `causiq.tools.airflow.DAG_ID_PATTERN` uses. Excluding `/`, `\`, `.`, and
#: whitespace outright means no string that satisfies it could ever be
#: mistaken for, or misused as, a path fragment - defense in depth, even
#: though `run()` never builds a path from this value at all (see the module
#: docstring).
CHECK_NAME_PATTERN: Final = r"^[A-Za-z0-9_-]{1,200}$"
_CHECK_NAME_RE: Final = re.compile(CHECK_NAME_PATTERN)

#: Generous for an in-memory lookup with no I/O; matches
#: `causiq.tools.airflow.DEFAULT_TIMEOUT_SECONDS` and
#: `causiq.tools.dbt.DEFAULT_TIMEOUT_SECONDS`.
DEFAULT_TIMEOUT_SECONDS: Final = 5.0


class DqCheckResultsInput(ToolInput):
    """Arguments for `query_dq_check_results`.

    Deliberately a narrow, closed lookup, exactly like
    `causiq.tools.airflow.AirflowRunsInput`/`causiq.tools.dbt.DbtRunResultsInput` -
    one bounded identifier, no query language, no date-range filter (P1
    architecture plan §6). One check shape (freshness) is all P1.3 needs.
    """

    check_name: str = Field(
        min_length=1,
        max_length=200,
        pattern=CHECK_NAME_PATTERN,
        description=(
            "The data-quality check's name, e.g. 'revenue_daily_freshness'. "
            "Letters, digits, underscore and hyphen only."
        ),
    )


class DqCheckResultsTool:
    """Read-only lookup of data-quality (freshness) check result history.

    One instance loads the fixture once, at construction, and is safe to
    register once for the life of a process - every call is a pure read
    against the in-memory index built at that time (see `DqCheckIndex`).
    """

    def __init__(
        self,
        name: str = "query_dq_check_results",
        *,
        fixture_path: Path = DEFAULT_DQ_FIXTURE_PATH,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._index = DqCheckIndex.load(fixture_path)
        self._spec = ToolSpec(
            name=name,
            description=(
                "Look up data-quality freshness check result history for "
                "one named check. Returns every recorded execution of that "
                "check, its status (pass or fail), the measured freshness, "
                "and the threshold it was checked against. State a clear "
                "reason: what you expect this to show and why."
            ),
            permission=Permission.ARTIFACTS_READ,
            mutating=False,
            risk=RiskLevel.LOW,
            source=EvidenceSource.DATA_QUALITY,
            timeout_seconds=timeout_seconds,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def input_model(self) -> type[ToolInput]:
        return DqCheckResultsInput

    def run(self, payload: ToolInput) -> ToolOutput:
        """Look up `payload.check_name` in the in-memory index.

        Raises `causiq.errors.ToolExecutionError` for an unknown (but
        well-formed - schema validation already rejected anything else)
        `check_name`; the executor classifies that as an ordinary tool
        failure (`ToolResultStatus.FAILED`), audits it, and records no
        evidence - exactly the same path a failed `query_dbt_run_results`
        call takes.
        """
        assert isinstance(payload, DqCheckResultsInput)
        check = self._index.get(payload.check_name)
        if check is None:
            known = ", ".join(sorted(self._index.check_names())) or "(none)"
            msg = f"no DQ check named {payload.check_name!r} in the evidence substrate"
            raise ToolExecutionError(msg, check_name=payload.check_name, known_checks=known)

        content = canonical_json(check.model_dump(mode="json"))
        return ToolOutput(content=content, content_type="application/json")


__all__ = ["CHECK_NAME_PATTERN", "DqCheckResultsInput", "DqCheckResultsTool"]
