"""`AirflowDagRunsTool` - the tool's own contract, in isolation (P1.1).

Split from `tests/integration/test_airflow_evidence.py` the same way
`test_warehouse_tool.py` is split from the warehouse's integration test: this
file is about the tool's own behavior (input validation, lookup, output
shape); the integration test proves the *only* way its output becomes
Evidence is through `ToolExecutor`.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from causiq.authz import Permission
from causiq.domain import EvidenceSource
from causiq.errors import ToolExecutionError
from causiq.tools.airflow import AirflowDagRunsTool, AirflowRunsInput

pytestmark = pytest.mark.unit


@pytest.fixture
def tool() -> AirflowDagRunsTool:
    return AirflowDagRunsTool()


def _input(**overrides: object) -> AirflowRunsInput:
    payload: dict[str, object] = {"dag_id": "daily_revenue_pipeline", "reason": "investigate"}
    payload.update(overrides)
    return AirflowRunsInput.model_validate(payload)


# --------------------------------------------------------------------------- #
# ToolSpec properties
# --------------------------------------------------------------------------- #
def test_spec_declares_artifacts_read_and_is_not_mutating(tool: AirflowDagRunsTool) -> None:
    assert tool.spec.permission == Permission.ARTIFACTS_READ
    assert tool.spec.mutating is False
    assert tool.spec.source == EvidenceSource.AIRFLOW
    assert tool.spec.name == "query_airflow_runs"
    assert tool.spec.timeout_seconds > 0


def test_input_model_is_airflow_runs_input(tool: AirflowDagRunsTool) -> None:
    assert tool.input_model is AirflowRunsInput


# --------------------------------------------------------------------------- #
# Valid lookup and result shape
# --------------------------------------------------------------------------- #
def test_valid_dag_lookup_succeeds(tool: AirflowDagRunsTool) -> None:
    output = tool.run(_input(dag_id="daily_revenue_pipeline"))
    assert output.content_type == "application/json"
    assert output.truncated is False

    parsed = json.loads(output.content)
    assert parsed["dag_id"] == "daily_revenue_pipeline"
    assert len(parsed["runs"]) >= 1
    run = parsed["runs"][0]
    assert {"dag_run_id", "execution_date", "start_date", "end_date", "state", "tasks"} <= set(run)
    assert len(run["tasks"]) >= 1
    task = run["tasks"][0]
    assert {"task_id", "state", "start_date", "end_date"} <= set(task)


def test_output_is_deterministic_across_calls(tool: AirflowDagRunsTool) -> None:
    first = tool.run(_input()).content
    second = tool.run(_input()).content
    assert first == second


def test_second_dag_in_the_fixture_is_also_reachable(tool: AirflowDagRunsTool) -> None:
    output = tool.run(_input(dag_id="customer_ltv_pipeline"))
    parsed = json.loads(output.content)
    assert parsed["dag_id"] == "customer_ltv_pipeline"


# --------------------------------------------------------------------------- #
# Unknown but well-formed DAG id -> ordinary tool failure, not a crash
# --------------------------------------------------------------------------- #
def test_unknown_but_well_formed_dag_id_raises_tool_execution_error(
    tool: AirflowDagRunsTool,
) -> None:
    with pytest.raises(ToolExecutionError, match="no Airflow DAG named"):
        tool.run(_input(dag_id="not_a_real_dag"))


def test_unknown_dag_error_names_the_dags_that_do_exist(tool: AirflowDagRunsTool) -> None:
    with pytest.raises(ToolExecutionError) as caught:
        tool.run(_input(dag_id="not_a_real_dag"))
    assert "daily_revenue_pipeline" in str(caught.value.context.get("known_dags", ""))


# --------------------------------------------------------------------------- #
# Malformed / hostile dag_id - rejected at the schema level, before run()
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "hostile_dag_id",
    [
        "../etc/passwd",
        "../../secrets",
        "..\\..\\windows\\system32",
        "/etc/passwd",
        "C:\\Windows\\System32",
        "a/b",
        "a\\b",
        "dag id with spaces",
        "dag\nid",
        "dag;id",
        "",
    ],
)
def test_path_traversal_and_malformed_dag_ids_are_rejected_at_the_schema_level(
    hostile_dag_id: str,
) -> None:
    with pytest.raises(ValidationError):
        AirflowRunsInput.model_validate({"dag_id": hostile_dag_id, "reason": "r"})


def test_run_never_touches_the_filesystem(
    tool: AirflowDagRunsTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empirical proof, not just an inference from reading the code: sever
    file access entirely, then run both a successful and a failing lookup.
    If `run()` ever opened, joined, or resolved a path derived from
    `dag_id`, this would raise instead of passing."""

    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        msg = "AirflowDagRunsTool.run() must not touch the filesystem"
        raise AssertionError(msg)

    monkeypatch.setattr("builtins.open", forbidden)

    output = tool.run(_input(dag_id="daily_revenue_pipeline"))
    assert json.loads(output.content)["dag_id"] == "daily_revenue_pipeline"

    with pytest.raises(ToolExecutionError):
        tool.run(_input(dag_id="not_a_real_dag"))


def test_overlong_dag_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AirflowRunsInput.model_validate({"dag_id": "a" * 201, "reason": "r"})


def test_valid_dag_id_shapes_are_accepted() -> None:
    for value in ("daily_revenue_pipeline", "customer_ltv_pipeline", "a", "A1_2-3"):
        parsed = AirflowRunsInput.model_validate({"dag_id": value, "reason": "r"})
        assert parsed.dag_id == value


def test_input_requires_a_reason() -> None:
    with pytest.raises(ValidationError):
        AirflowRunsInput.model_validate({"dag_id": "daily_revenue_pipeline"})
