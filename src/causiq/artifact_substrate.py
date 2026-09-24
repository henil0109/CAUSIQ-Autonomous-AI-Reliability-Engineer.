"""The Airflow artifact substrate: a static fixture, loaded into validated
in-memory structures (P1.1, ADR-0009).

Maturity: hardened.

This module is the artifact-fixture counterpart to `causiq.evidence_substrate`
(which owns the DuckDB warehouse substrate). The two differ in one important
way: the warehouse is *built* by executing a SQL script against a fresh file;
an artifact fixture is not built at all - it is a static JSON file, already
committed to the repository (ADR-0003: "artifacts committed to the repository"),
so this module's only job is to read, parse, and validate it, never to
generate or mutate it.

**The security property this module exists to guarantee:** a model-supplied
value can never become a filesystem path. The fixture is always read from
`DEFAULT_AIRFLOW_FIXTURE_PATH`, a constant fixed at import time - nothing in
this module or in `causiq.tools.airflow` ever joins, formats, or otherwise
builds a path from a `ToolInput` field. Once loaded, every subsequent lookup
(`AirflowDagIndex.get`) is a plain `dict` lookup keyed by a string that has
already passed `AirflowRunsInput`'s pattern validation - a dict miss for an
unexpected key, never an attempt to open, list, or stat anything.

Deliberately not a generic "artifact framework": this module knows about
exactly one shape (Airflow DAG runs and their task instances). A second
artifact source (dbt, git, deployment records, DQ results - P1.2+) gets its
own small loader when it lands, following this one as a pattern rather than
being forced through a shared abstraction invented before a second concrete
user exists to justify it (see ADR-0009).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from causiq.errors import ConfigurationError

#: Repository root, derived from this file's location - matches
#: `causiq.evidence_substrate.REPO_ROOT` exactly, for the same reason: callers
#: get the same fixture regardless of the current working directory.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_AIRFLOW_FIXTURE_PATH = REPO_ROOT / "fixtures" / "artifacts" / "airflow" / "dag_runs.json"

#: Airflow's own closed state vocabulary (a subset of the real states worth
#: representing for Phase 1 - this is Airflow's vocabulary being modeled
#: faithfully, not a Causiq domain concept, so it lives here rather than in
#: `causiq.domain.enums`).
AirflowState = Literal[
    "success",
    "failed",
    "upstream_failed",
    "skipped",
    "running",
    "queued",
    "up_for_retry",
]


def _require_timezone(value: datetime) -> datetime:
    """Shared validator: every Airflow timestamp must be timezone-aware.

    A naive timestamp here would make cross-source timeline correlation
    (Engineering Contract / P1 plan §5) ambiguous - the same reason
    `causiq.domain.incident.Incident.detected_at` enforces it.
    """
    if value.tzinfo is None:
        msg = "Airflow fixture timestamps must be timezone-aware"
        raise ValueError(msg)
    return value


class AirflowTaskInstance(BaseModel):
    """One task's recorded execution within one DAG run. Immutable, facts only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    state: AirflowState
    start_date: datetime
    end_date: datetime

    _validate_start = field_validator("start_date")(_require_timezone)
    _validate_end = field_validator("end_date")(_require_timezone)


class AirflowDagRun(BaseModel):
    """One recorded run of a DAG, with every task instance it produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dag_run_id: str = Field(min_length=1)
    execution_date: datetime
    start_date: datetime
    end_date: datetime
    state: AirflowState
    tasks: tuple[AirflowTaskInstance, ...] = Field(min_length=1)

    _validate_execution = field_validator("execution_date")(_require_timezone)
    _validate_start = field_validator("start_date")(_require_timezone)
    _validate_end = field_validator("end_date")(_require_timezone)


class AirflowDag(BaseModel):
    """One DAG's full recorded run history in the fixture."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dag_id: str = Field(min_length=1)
    runs: tuple[AirflowDagRun, ...] = Field(min_length=1)


class AirflowFixture(BaseModel):
    """The whole committed Airflow fixture: every DAG it describes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dags: tuple[AirflowDag, ...] = Field(min_length=1)

    @field_validator("dags")
    @classmethod
    def _dag_ids_are_unique(cls, value: tuple[AirflowDag, ...]) -> tuple[AirflowDag, ...]:
        """A duplicate `dag_id` would make the in-memory index ambiguous -
        reject it at load time rather than silently keeping only the last one."""
        ids = [dag.dag_id for dag in value]
        if len(ids) != len(set(ids)):
            msg = "the Airflow fixture contains a duplicate dag_id"
            raise ValueError(msg)
        return value


def load_airflow_fixture(path: Path = DEFAULT_AIRFLOW_FIXTURE_PATH) -> AirflowFixture:
    """Read, parse, and validate the committed Airflow fixture.

    Raises `causiq.errors.ConfigurationError` for anything wrong with the
    fixture itself (missing file, malformed JSON, a structure that fails
    validation) - this is a startup-shaped failure, exactly like a missing
    `ANTHROPIC_API_KEY`, never something a model's tool call could trigger,
    since `path` is never derived from a request.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"could not read the Airflow fixture at {path}"
        raise ConfigurationError(msg, path=str(path)) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"the Airflow fixture at {path} is not valid JSON"
        raise ConfigurationError(msg, path=str(path)) from exc
    try:
        return AirflowFixture.model_validate(data)
    except PydanticValidationError as exc:
        msg = f"the Airflow fixture at {path} does not match the expected structure"
        raise ConfigurationError(msg, path=str(path), problems=str(exc)) from exc


class AirflowDagIndex:
    """An in-memory, validated index of the Airflow fixture, keyed by `dag_id`.

    This is the *only* thing `causiq.tools.airflow.AirflowDagRunsTool.run()`
    ever consults. It is built once, from the fixed fixture path, and every
    subsequent lookup is a plain `dict.get` - there is no code path here or in
    the tool by which a model-supplied string reaches the filesystem.
    """

    def __init__(self, fixture: AirflowFixture) -> None:
        self._dags: dict[str, AirflowDag] = {dag.dag_id: dag for dag in fixture.dags}

    @classmethod
    def load(cls, path: Path = DEFAULT_AIRFLOW_FIXTURE_PATH) -> Self:
        return cls(load_airflow_fixture(path))

    def get(self, dag_id: str) -> AirflowDag | None:
        """The DAG named `dag_id`, or `None` if it is not in the fixture.

        Never raises for a miss, matching `causiq.evidence.EvidenceLedger.get`'s
        convention - a miss is an ordinary, expected outcome for the caller to
        handle, not an exceptional one.
        """
        return self._dags.get(dag_id)

    def dag_ids(self) -> frozenset[str]:
        """Every DAG id this index holds - used to help the model self-correct
        after an unknown lookup, the same courtesy `ToolRegistry` extends for
        an unknown tool name."""
        return frozenset(self._dags)
