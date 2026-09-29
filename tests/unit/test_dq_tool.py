"""`DqCheckResultsTool` - the tool's own contract, in isolation (P1.3).

Mirrors `test_dbt_tool.py`'s structure: this file is about the tool's own
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
from causiq.tools.dq import DqCheckResultsInput, DqCheckResultsTool

pytestmark = pytest.mark.unit


@pytest.fixture
def tool() -> DqCheckResultsTool:
    return DqCheckResultsTool()


def _input(**overrides: object) -> DqCheckResultsInput:
    payload: dict[str, object] = {
        "check_name": "revenue_daily_freshness",
        "reason": "investigate",
    }
    payload.update(overrides)
    return DqCheckResultsInput.model_validate(payload)


# --------------------------------------------------------------------------- #
# ToolSpec properties
# --------------------------------------------------------------------------- #
def test_spec_declares_artifacts_read_and_is_not_mutating(tool: DqCheckResultsTool) -> None:
    assert tool.spec.permission == Permission.ARTIFACTS_READ
    assert tool.spec.mutating is False
    assert tool.spec.source == EvidenceSource.DATA_QUALITY
    assert tool.spec.risk == RiskLevel.LOW
    assert tool.spec.name == "query_dq_check_results"
    assert tool.spec.timeout_seconds > 0


def test_input_model_is_dq_check_results_input(tool: DqCheckResultsTool) -> None:
    assert tool.input_model is DqCheckResultsInput


# --------------------------------------------------------------------------- #
# Valid lookup and result shape
# --------------------------------------------------------------------------- #
def test_valid_check_lookup_succeeds(tool: DqCheckResultsTool) -> None:
    output = tool.run(_input(check_name="revenue_daily_freshness"))
    assert output.content_type == "application/json"
    assert output.truncated is False

    parsed = json.loads(output.content)
    assert parsed["check_name"] == "revenue_daily_freshness"
    assert parsed["dataset"] == "analytics.revenue_daily"
    assert len(parsed["results"]) >= 1
    result = parsed["results"][0]
    assert {"checked_at", "status", "measured_freshness_minutes", "threshold_minutes"} <= set(
        result
    )


def test_output_is_deterministic_across_calls(tool: DqCheckResultsTool) -> None:
    first = tool.run(_input()).content
    second = tool.run(_input()).content
    assert first == second


def test_other_checks_in_the_fixture_are_also_reachable(tool: DqCheckResultsTool) -> None:
    output = tool.run(_input(check_name="customer_ltv_freshness"))
    parsed = json.loads(output.content)
    assert parsed["check_name"] == "customer_ltv_freshness"


def test_result_shows_the_failing_freshness_check(tool: DqCheckResultsTool) -> None:
    """Sanity check on the fixture's own content, from the tool's own
    output - not a claim about the incident, just proof the fail case is
    actually reachable through this tool."""
    output = tool.run(_input(check_name="revenue_daily_freshness"))
    parsed = json.loads(output.content)
    statuses = {result["status"] for result in parsed["results"]}
    assert "fail" in statuses
    assert "pass" in statuses


# --------------------------------------------------------------------------- #
# Unknown but well-formed check_name -> ordinary tool failure, not a crash
# --------------------------------------------------------------------------- #
def test_unknown_but_well_formed_check_name_raises_tool_execution_error(
    tool: DqCheckResultsTool,
) -> None:
    with pytest.raises(ToolExecutionError, match="no DQ check named"):
        tool.run(_input(check_name="not_a_real_check"))


def test_unknown_check_error_names_the_checks_that_do_exist(tool: DqCheckResultsTool) -> None:
    with pytest.raises(ToolExecutionError) as caught:
        tool.run(_input(check_name="not_a_real_check"))
    assert "revenue_daily_freshness" in str(caught.value.context.get("known_checks", ""))


# --------------------------------------------------------------------------- #
# Malformed / hostile check_name - rejected at the schema level, before run()
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "hostile_check_name",
    [
        "../etc/passwd",
        "../../secrets",
        "..\\..\\windows\\system32",
        "/etc/passwd",
        "C:\\Windows\\System32",
        "a/b",
        "a\\b",
        "check id with spaces",
        "check\nid",
        "check;id",
        "check.id",
        "",
    ],
)
def test_path_traversal_and_malformed_check_names_are_rejected_at_the_schema_level(
    hostile_check_name: str,
) -> None:
    with pytest.raises(ValidationError):
        DqCheckResultsInput.model_validate({"check_name": hostile_check_name, "reason": "r"})


def test_run_never_touches_the_filesystem(
    tool: DqCheckResultsTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empirical proof, not just an inference from reading the code: sever
    file access entirely, then run both a successful and a failing lookup.
    If `run()` ever opened, joined, or resolved a path derived from
    `check_name`, this would raise instead of passing."""

    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        msg = "DqCheckResultsTool.run() must not touch the filesystem"
        raise AssertionError(msg)

    monkeypatch.setattr("builtins.open", forbidden)

    output = tool.run(_input(check_name="revenue_daily_freshness"))
    assert json.loads(output.content)["check_name"] == "revenue_daily_freshness"

    with pytest.raises(ToolExecutionError):
        tool.run(_input(check_name="not_a_real_check"))


def test_overlong_check_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DqCheckResultsInput.model_validate({"check_name": "a" * 201, "reason": "r"})


def test_valid_check_name_shapes_are_accepted() -> None:
    for value in ("revenue_daily_freshness", "customer_ltv_freshness", "a", "a_b-c", "A1_2"):
        parsed = DqCheckResultsInput.model_validate({"check_name": value, "reason": "r"})
        assert parsed.check_name == value


def test_input_requires_a_reason() -> None:
    with pytest.raises(ValidationError):
        DqCheckResultsInput.model_validate({"check_name": "revenue_daily_freshness"})
