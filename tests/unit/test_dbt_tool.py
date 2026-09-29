"""`DbtRunResultsTool` - the tool's own contract, in isolation (P1.2).

Mirrors `test_airflow_tool.py`'s structure: this file is about the tool's own
behavior (input validation, lookup, output shape); the integration test
proves the *only* way its output becomes Evidence is through `ToolExecutor`.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from causiq.authz import Permission
from causiq.domain import EvidenceSource, RiskLevel
from causiq.errors import ToolExecutionError
from causiq.tools.dbt import DbtRunResultsInput, DbtRunResultsTool

pytestmark = pytest.mark.unit


@pytest.fixture
def tool() -> DbtRunResultsTool:
    return DbtRunResultsTool()


def _input(**overrides: object) -> DbtRunResultsInput:
    payload: dict[str, object] = {
        "unique_id": "model.revenue_analytics.daily_revenue_pipeline",
        "reason": "investigate",
    }
    payload.update(overrides)
    return DbtRunResultsInput.model_validate(payload)


# --------------------------------------------------------------------------- #
# ToolSpec properties
# --------------------------------------------------------------------------- #
def test_spec_declares_artifacts_read_and_is_not_mutating(tool: DbtRunResultsTool) -> None:
    assert tool.spec.permission == Permission.ARTIFACTS_READ
    assert tool.spec.mutating is False
    assert tool.spec.source == EvidenceSource.DBT
    assert tool.spec.risk == RiskLevel.LOW
    assert tool.spec.name == "query_dbt_run_results"
    assert tool.spec.timeout_seconds > 0


def test_input_model_is_dbt_run_results_input(tool: DbtRunResultsTool) -> None:
    assert tool.input_model is DbtRunResultsInput


# --------------------------------------------------------------------------- #
# Valid lookup and result shape
# --------------------------------------------------------------------------- #
def test_valid_model_lookup_succeeds(tool: DbtRunResultsTool) -> None:
    output = tool.run(_input(unique_id="model.revenue_analytics.daily_revenue_pipeline"))
    assert output.content_type == "application/json"
    assert output.truncated is False

    parsed = json.loads(output.content)
    assert parsed["unique_id"] == "model.revenue_analytics.daily_revenue_pipeline"
    assert len(parsed["results"]) >= 1
    result = parsed["results"][0]
    assert {"invocation_id", "generated_at", "status", "execution_time"} <= set(result)


def test_output_is_deterministic_across_calls(tool: DbtRunResultsTool) -> None:
    first = tool.run(_input()).content
    second = tool.run(_input()).content
    assert first == second


def test_other_models_in_the_fixture_are_also_reachable(tool: DbtRunResultsTool) -> None:
    for unique_id in (
        "model.revenue_analytics.stg_orders",
        "model.revenue_analytics.customer_ltv",
    ):
        output = tool.run(_input(unique_id=unique_id))
        parsed = json.loads(output.content)
        assert parsed["unique_id"] == unique_id


# --------------------------------------------------------------------------- #
# Unknown but well-formed unique_id -> ordinary tool failure, not a crash
# --------------------------------------------------------------------------- #
def test_unknown_but_well_formed_unique_id_raises_tool_execution_error(
    tool: DbtRunResultsTool,
) -> None:
    with pytest.raises(ToolExecutionError, match="no dbt model named"):
        tool.run(_input(unique_id="model.pkg.not_a_real_model"))


def test_unknown_model_error_names_the_models_that_do_exist(tool: DbtRunResultsTool) -> None:
    with pytest.raises(ToolExecutionError) as caught:
        tool.run(_input(unique_id="model.pkg.not_a_real_model"))
    assert "model.revenue_analytics.daily_revenue_pipeline" in str(
        caught.value.context.get("known_models", "")
    )


# --------------------------------------------------------------------------- #
# Malformed / hostile unique_id - rejected at the schema level, before run()
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "hostile_unique_id",
    [
        "../etc/passwd",
        "../../secrets",
        "..\\..\\windows\\system32",
        "/etc/passwd",
        "C:\\Windows\\System32",
        "a/b",
        "a\\b",
        "..",
        ".leading",
        "trailing.",
        "model..pkg.m",
        "model id with spaces",
        "model\nid",
        "model;id",
        "",
    ],
)
def test_path_traversal_and_malformed_unique_ids_are_rejected_at_the_schema_level(
    hostile_unique_id: str,
) -> None:
    with pytest.raises(ValidationError):
        DbtRunResultsInput.model_validate({"unique_id": hostile_unique_id, "reason": "r"})


def test_run_never_touches_the_filesystem(
    tool: DbtRunResultsTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empirical proof, not just an inference from reading the code: sever
    file access entirely, then run both a successful and a failing lookup.
    If `run()` ever opened, joined, or resolved a path derived from
    `unique_id`, this would raise instead of passing."""

    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        msg = "DbtRunResultsTool.run() must not touch the filesystem"
        raise AssertionError(msg)

    monkeypatch.setattr("builtins.open", forbidden)

    output = tool.run(_input(unique_id="model.revenue_analytics.daily_revenue_pipeline"))
    assert (
        json.loads(output.content)["unique_id"] == "model.revenue_analytics.daily_revenue_pipeline"
    )

    with pytest.raises(ToolExecutionError):
        tool.run(_input(unique_id="model.pkg.not_a_real_model"))


def test_overlong_unique_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DbtRunResultsInput.model_validate({"unique_id": "a" * 201, "reason": "r"})


def test_valid_unique_id_shapes_are_accepted() -> None:
    for value in (
        "model.revenue_analytics.daily_revenue_pipeline",
        "model.revenue_analytics.customer_ltv",
        "a",
        "a.b",
        "a.b.c",
        "A1_2.B3_4",
    ):
        parsed = DbtRunResultsInput.model_validate({"unique_id": value, "reason": "r"})
        assert parsed.unique_id == value


def test_hyphenated_unique_id_is_rejected() -> None:
    """dbt's real identifier grammar does not use hyphens between segments -
    the pattern is stricter than strictly necessary here, which is the safe
    direction to err in."""
    with pytest.raises(ValidationError):
        DbtRunResultsInput.model_validate({"unique_id": "model.pkg.a-b", "reason": "r"})


def test_input_requires_a_reason() -> None:
    with pytest.raises(ValidationError):
        DbtRunResultsInput.model_validate({"unique_id": "model.revenue_analytics.stg_orders"})
