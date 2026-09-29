"""The dbt artifact substrate: a static fixture, loaded into validated
in-memory structures (P1.2, ADR-0009 amendment).

Maturity: hardened.

This module is `causiq.artifact_substrate`'s sibling for dbt, not an
extension of it: ADR-0009 already decided that a second artifact source gets
its own small loader, following that module's shape as a pattern rather than
being merged into it or forced through a shared abstraction. Two small,
single-purpose files are more explainable than one file mixing two unrelated
fixture shapes.

**The security property this module exists to guarantee** (identical to
`causiq.artifact_substrate`'s): a model-supplied value can never become a
filesystem path. The fixture is always read from `DEFAULT_DBT_FIXTURE_PATH`, a
constant fixed at import time - nothing here or in `causiq.tools.dbt` ever
joins, formats, or otherwise builds a path from a `ToolInput` field. Once
loaded, every lookup (`DbtModelIndex.get`) is a plain `dict` lookup keyed by a
string that has already passed `DbtRunResultsInput`'s pattern validation.

The on-disk fixture groups results by model (`unique_id` -> its own `results`
list) rather than mirroring real dbt's per-invocation `run_results.json`
layout, purely for lookup parity with `causiq.artifact_substrate`'s
`dag_id -> runs` shape. This is a fixture-organization choice, not a realism
loss: a production adapter reading real per-invocation `run_results.json`
files would perform this same regrouping internally before indexing by
model. Field names (`unique_id`, `status`, `execution_time`, `generated_at`)
are dbt's own.
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
#: `causiq.evidence_substrate.REPO_ROOT` exactly, for the same reason: callers
#: get the same fixture regardless of the current working directory.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_DBT_FIXTURE_PATH = REPO_ROOT / "fixtures" / "artifacts" / "dbt" / "run_results.json"

#: dbt's own closed model-run status vocabulary (the subset relevant to
#: reliability reasoning - test-result statuses like `fail`/`warn` describe
#: dbt *tests*, not model runs, and are out of scope here).
DbtRunStatus = Literal["success", "error", "skipped"]


def _require_timezone(value: datetime) -> datetime:
    """Shared validator: every dbt timestamp must be timezone-aware - the
    same reason `causiq.artifact_substrate._require_timezone` enforces it."""
    if value.tzinfo is None:
        msg = "dbt fixture timestamps must be timezone-aware"
        raise ValueError(msg)
    return value


class DbtModelResult(BaseModel):
    """One recorded invocation's outcome for one model. Immutable, facts only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: str = Field(min_length=1)
    generated_at: datetime
    status: DbtRunStatus
    execution_time: float = Field(ge=0.0)

    _validate_generated_at = field_validator("generated_at")(_require_timezone)


class DbtModel(BaseModel):
    """One dbt model's full recorded result history in the fixture."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unique_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    results: tuple[DbtModelResult, ...] = Field(min_length=1)


class DbtFixture(BaseModel):
    """The whole committed dbt fixture: every model it describes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    models: tuple[DbtModel, ...] = Field(min_length=1)

    @field_validator("models")
    @classmethod
    def _unique_ids_are_unique(cls, value: tuple[DbtModel, ...]) -> tuple[DbtModel, ...]:
        """A duplicate `unique_id` would make the in-memory index ambiguous -
        reject it at load time rather than silently keeping only the last one."""
        ids = [model.unique_id for model in value]
        if len(ids) != len(set(ids)):
            msg = "the dbt fixture contains a duplicate unique_id"
            raise ValueError(msg)
        return value


def load_dbt_fixture(path: Path = DEFAULT_DBT_FIXTURE_PATH) -> DbtFixture:
    """Read, parse, and validate the committed dbt fixture.

    Raises `causiq.errors.ConfigurationError` for anything wrong with the
    fixture itself - a startup-shaped failure, never something a model's tool
    call could trigger, since `path` is never derived from a request.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"could not read the dbt fixture at {path}"
        raise ConfigurationError(msg, path=str(path)) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"the dbt fixture at {path} is not valid JSON"
        raise ConfigurationError(msg, path=str(path)) from exc
    try:
        return DbtFixture.model_validate(data)
    except PydanticValidationError as exc:
        msg = f"the dbt fixture at {path} does not match the expected structure"
        raise ConfigurationError(msg, path=str(path), problems=str(exc)) from exc


class DbtModelIndex:
    """An in-memory, validated index of the dbt fixture, keyed by `unique_id`.

    This is the *only* thing `causiq.tools.dbt.DbtRunResultsTool.run()` ever
    consults. It is built once, from the fixed fixture path, and every
    subsequent lookup is a plain `dict.get` - there is no code path here or
    in the tool by which a model-supplied string reaches the filesystem.
    """

    def __init__(self, fixture: DbtFixture) -> None:
        self._models: dict[str, DbtModel] = {model.unique_id: model for model in fixture.models}

    @classmethod
    def load(cls, path: Path = DEFAULT_DBT_FIXTURE_PATH) -> Self:
        return cls(load_dbt_fixture(path))

    def get(self, unique_id: str) -> DbtModel | None:
        """The model named `unique_id`, or `None` if it is not in the
        fixture. Never raises for a miss - a miss is an ordinary, expected
        outcome for the caller to handle, matching
        `causiq.artifact_substrate.AirflowDagIndex.get`'s convention."""
        return self._models.get(unique_id)

    def unique_ids(self) -> frozenset[str]:
        """Every model id this index holds - used to help the model
        self-correct after an unknown lookup."""
        return frozenset(self._models)
