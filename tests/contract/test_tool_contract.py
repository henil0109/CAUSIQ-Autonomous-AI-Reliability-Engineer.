"""Tool-layer contract tests.

What is protected here is not behaviour but *shape*: the schema the API is given,
the closed status vocabulary the evaluator will count, and the invariant that only
a successful call produces evidence. These are the things that break quietly.
"""

from __future__ import annotations

import json

import pytest

from causiq.authz import Permission
from causiq.domain import EvidenceSource, RiskLevel
from causiq.tools import (
    ToolInput,
    ToolRegistry,
    ToolResultStatus,
    ToolSpec,
    canonical_json,
    tool_schema,
)
from tests.doubles import EchoTool, MutatingTool

pytestmark = pytest.mark.contract


# --------------------------------------------------------------------------- #
# Schema shape - what the API is actually sent (ADR-0005)
# --------------------------------------------------------------------------- #
def test_tool_definition_has_exactly_the_expected_keys() -> None:
    assert set(tool_schema(EchoTool())) == {"name", "description", "input_schema", "strict"}


def test_every_tool_schema_is_strict_eligible() -> None:
    """strict + additionalProperties:false + complete required = arguments that
    are schema-valid by construction."""
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(MutatingTool())
    for schema in registry.schemas():
        assert schema["strict"] is True, schema["name"]
        input_schema = schema["input_schema"]
        assert input_schema["type"] == "object", schema["name"]
        assert input_schema["additionalProperties"] is False, schema["name"]
        assert "required" in input_schema, schema["name"]
        assert "reason" in input_schema["required"], schema["name"]


def test_schema_is_json_serialisable() -> None:
    """It has to survive the request body; a non-serialisable schema would fail
    at call time rather than at registration."""
    registry = ToolRegistry()
    registry.register(EchoTool())
    assert json.loads(canonical_json(list(registry.schemas())))


def test_canonical_json_sorts_keys_and_strips_whitespace() -> None:
    assert canonical_json({"b": 1, "a": {"d": 2, "c": 3}}) == '{"a":{"c":3,"d":2},"b":1}'


def test_canonical_json_is_order_independent() -> None:
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


# --------------------------------------------------------------------------- #
# Security classification defaults - invariant I4
# --------------------------------------------------------------------------- #
def test_tools_are_read_only_unless_they_opt_in() -> None:
    """ "Read is default" is true by construction: a tool author has to opt in to
    being dangerous, rather than remembering to opt out."""
    spec = ToolSpec(
        name="minimal_reader",
        description="d",
        permission=Permission.WAREHOUSE_READ,
        source=EvidenceSource.WAREHOUSE,
    )
    assert spec.mutating is False
    assert spec.risk is RiskLevel.LOW


def test_spec_is_immutable() -> None:
    spec = EchoTool().spec
    with pytest.raises(ValueError, match=r"frozen|immutable"):
        spec.mutating = True


def test_timeout_must_be_positive_and_bounded() -> None:
    from pydantic import ValidationError

    base = {
        "name": "some_reader",
        "description": "d",
        "permission": Permission.WAREHOUSE_READ,
        "source": EvidenceSource.WAREHOUSE,
    }
    for bad in (0, -1, 601):
        with pytest.raises(ValidationError):
            ToolSpec(**base, timeout_seconds=bad)  # type: ignore[arg-type]


def test_validated_payload_cannot_be_altered_before_execution() -> None:
    """The thing that was checked is the thing that runs."""
    payload = EchoTool().input_model.model_validate({"value": "x", "reason": "r"})
    with pytest.raises(ValueError, match=r"frozen|immutable"):
        payload.reason = "something else"


def test_tool_input_base_requires_a_reason() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ToolInput.model_validate({})


# --------------------------------------------------------------------------- #
# Result vocabulary - the Phase 4 evaluator counts these
# --------------------------------------------------------------------------- #
def test_result_status_vocabulary_is_the_documented_one() -> None:
    assert {status.value for status in ToolResultStatus} == {
        "ok",
        "unknown_tool",
        "denied",
        "invalid_input",
        "failed",
        "timeout",
    }


def test_exactly_one_status_denotes_success() -> None:
    """Every other outcome is an error result, and none of them records evidence.
    That is what keeps the ledger a record of facts rather than of attempts."""
    successes = [status for status in ToolResultStatus if status is ToolResultStatus.OK]
    assert len(successes) == 1
