"""The data-quality artifact substrate: a static fixture, loaded into
validated in-memory structures (P1.3, ADR-0009 amendment).

Maturity: hardened.

This module is `causiq.artifact_substrate`/`causiq.dbt_substrate`'s sibling
for data-quality check results, not an extension of either: ADR-0009 already
decided that each artifact source gets its own small loader, following the
first one's shape as a pattern rather than being merged into a shared module.
Three small, single-purpose loaders are more explainable than one file mixing
three unrelated fixture shapes.

**The security property this module exists to guarantee** (identical to
`causiq.artifact_substrate`'s and `causiq.dbt_substrate`'s): a model-supplied
value can never become a filesystem path. The fixture is always read from
`DEFAULT_DQ_FIXTURE_PATH`, a constant fixed at import time - nothing here or
in `causiq.tools.dq` ever joins, formats, or otherwise builds a path from a
`ToolInput` field. Once loaded, every lookup (`DqCheckIndex.get`) is a plain
`dict` lookup keyed by a string that has already passed
`DqCheckResultsInput`'s pattern validation.

Scope is deliberately narrow: one check shape (freshness - a dataset's
measured staleness against a threshold), not a general-purpose data-quality
rule engine. A future check type gets its own fixture/loader/tool when a
concrete incident needs one, following this module as a pattern, exactly as
this module followed `dbt_substrate.py`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from causiq.errors import ConfigurationError

#: Matches `causiq.artifact_substrate.REPO_ROOT` and
#: `causiq.dbt_substrate.REPO_ROOT` exactly, for the same reason: callers get
#: the same fixture regardless of the current working directory.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_DQ_FIXTURE_PATH = REPO_ROOT / "fixtures" / "artifacts" / "dq" / "check_results.json"

#: A freshness check's closed outcome vocabulary. Narrower than a general DQ
#: rule engine's status set (no `warn`/`error` distinct from `fail`) because
#: freshness is the only check shape this module represents.
DqCheckStatus = Literal["pass", "fail"]


def _require_timezone(value: datetime) -> datetime:
    """Shared validator: every DQ timestamp must be timezone-aware - the
    same reason `causiq.artifact_substrate._require_timezone` and
    `causiq.dbt_substrate._require_timezone` enforce it."""
    if value.tzinfo is None:
        msg = "DQ fixture timestamps must be timezone-aware"
        raise ValueError(msg)
    return value


class DqCheckResult(BaseModel):
    """One recorded execution of one freshness check. Immutable, facts only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checked_at: datetime
    status: DqCheckStatus
    measured_freshness_minutes: float = Field(ge=0.0)
    threshold_minutes: float = Field(gt=0.0)

    _validate_checked_at = field_validator("checked_at")(_require_timezone)


class DqCheck(BaseModel):
    """One named check's full recorded result history in the fixture."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_name: str = Field(min_length=1)
    dataset: str = Field(min_length=1)
    results: tuple[DqCheckResult, ...] = Field(min_length=1)


class DqFixture(BaseModel):
    """The whole committed DQ fixture: every check it describes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checks: tuple[DqCheck, ...] = Field(min_length=1)

    @field_validator("checks")
    @classmethod
    def _check_names_are_unique(cls, value: tuple[DqCheck, ...]) -> tuple[DqCheck, ...]:
        """A duplicate `check_name` would make the in-memory index ambiguous -
        reject it at load time rather than silently keeping only the last one."""
        names = [check.check_name for check in value]
        if len(names) != len(set(names)):
            msg = "the DQ fixture contains a duplicate check_name"
            raise ValueError(msg)
        return value


def load_dq_fixture(path: Path = DEFAULT_DQ_FIXTURE_PATH) -> DqFixture:
    """Read, parse, and validate the committed DQ fixture.

    Raises `causiq.errors.ConfigurationError` for anything wrong with the
    fixture itself - a startup-shaped failure, never something a model's
    tool call could trigger, since `path` is never derived from a request.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"could not read the DQ fixture at {path}"
        raise ConfigurationError(msg, path=str(path)) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"the DQ fixture at {path} is not valid JSON"
        raise ConfigurationError(msg, path=str(path)) from exc
    try:
        return DqFixture.model_validate(data)
    except PydanticValidationError as exc:
        msg = f"the DQ fixture at {path} does not match the expected structure"
        raise ConfigurationError(msg, path=str(path), problems=str(exc)) from exc


class DqCheckIndex:
    """An in-memory, validated index of the DQ fixture, keyed by `check_name`.

    This is the *only* thing `causiq.tools.dq.DqCheckResultsTool.run()` ever
    consults. It is built once, from the fixed fixture path, and every
    subsequent lookup is a plain `dict.get` - there is no code path here or
    in the tool by which a model-supplied string reaches the filesystem.
    """

    def __init__(self, fixture: DqFixture) -> None:
        self._checks: dict[str, DqCheck] = {check.check_name: check for check in fixture.checks}

    @classmethod
    def load(cls, path: Path = DEFAULT_DQ_FIXTURE_PATH) -> Self:
        return cls(load_dq_fixture(path))

    def get(self, check_name: str) -> DqCheck | None:
        """The check named `check_name`, or `None` if it is not in the
        fixture. Never raises for a miss - a miss is an ordinary, expected
        outcome for the caller to handle, matching
        `causiq.artifact_substrate.AirflowDagIndex.get`'s convention."""
        return self._checks.get(check_name)

    def check_names(self) -> frozenset[str]:
        """Every check name this index holds - used to help the model
        self-correct after an unknown lookup."""
        return frozenset(self._checks)
