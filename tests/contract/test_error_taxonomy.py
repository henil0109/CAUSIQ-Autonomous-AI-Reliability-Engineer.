"""The error taxonomy is a contract, not a convenience.

Engineering Contract 8 classifies every error by recoverability, because that
classification is what makes retry policy and run termination decidable. These
tests fail if someone adds an error without deciding which side it falls on.
"""

from __future__ import annotations

import pytest

from causiq.errors import (
    BudgetExceededError,
    CausiqError,
    ConfigurationError,
    DomainError,
    LedgerIntegrityError,
    ModelContractError,
    ModelError,
    ModelRefusalError,
    ModelTransientError,
    ToolAuthorizationError,
    ToolError,
    ToolExecutionError,
    ToolTimeoutError,
    UnresolvedCitationError,
    all_error_types,
)

pytestmark = pytest.mark.contract

#: The taxonomy as documented in Engineering Contract 8. Adding a class here is a
#: deliberate act; adding one without updating this list fails the build.
EXPECTED_TAXONOMY = {
    "BudgetExceededError",
    "CausiqError",
    "ConfigurationError",
    "DomainError",
    "LedgerIntegrityError",
    "ModelContractError",
    "ModelError",
    "ModelRefusalError",
    "ModelTransientError",
    "ToolAuthorizationError",
    "ToolError",
    "ToolExecutionError",
    "ToolTimeoutError",
    "UnresolvedCitationError",
}

RECOVERABLE = {
    ModelTransientError,
    ToolError,
    ToolAuthorizationError,
    ToolExecutionError,
    ToolTimeoutError,
}


def _class_name(cls: type[CausiqError]) -> str:
    """Sort key - keeps the parametrised ids stable across runs."""
    return cls.__name__


#: Bound to a typed name because parametrize's argvalues is Iterable[object],
#: which would otherwise erase the element type.
TAXONOMY: tuple[type[CausiqError], ...] = tuple(sorted(all_error_types(), key=_class_name))


def test_taxonomy_matches_the_contract() -> None:
    assert {cls.__name__ for cls in all_error_types()} == EXPECTED_TAXONOMY


def test_enumeration_is_deterministic() -> None:
    assert all_error_types() == all_error_types()


@pytest.mark.parametrize("error_type", TAXONOMY)
def test_every_error_declares_its_recoverability(error_type: type[CausiqError]) -> None:
    expected = error_type in RECOVERABLE
    assert error_type.recoverable is expected, (
        f"{error_type.__name__}.recoverable should be {expected}"
    )


def test_tool_failures_are_recoverable_so_the_agent_can_adapt() -> None:
    """A denied or failed tool becomes an is_error result, not a crashed run."""
    for error_type in (ToolAuthorizationError, ToolExecutionError, ToolTimeoutError):
        assert error_type.recoverable


def test_invariant_violations_are_fatal() -> None:
    """An unresolvable citation invalidates the conclusion - it is not a warning."""
    for error_type in (DomainError, UnresolvedCitationError, LedgerIntegrityError):
        assert not error_type.recoverable


def test_refusals_are_not_retried() -> None:
    """Retrying a policy decline is futile and dishonest."""
    assert not ModelRefusalError.recoverable
    assert ModelTransientError.recoverable


def test_hierarchy_is_as_documented() -> None:
    assert issubclass(UnresolvedCitationError, DomainError)
    assert issubclass(LedgerIntegrityError, DomainError)
    assert issubclass(ModelTransientError, ModelError)
    assert issubclass(ModelRefusalError, ModelError)
    assert issubclass(ModelContractError, ModelError)
    assert issubclass(ToolAuthorizationError, ToolError)
    assert issubclass(ConfigurationError, CausiqError)
    assert issubclass(BudgetExceededError, CausiqError)


def test_context_is_rendered_deterministically() -> None:
    """Sorted keys, so the same failure reads the same way every time."""
    error = ToolExecutionError("tool failed", tool="query_warehouse", attempt=2)
    assert str(error) == "tool failed (attempt=2, tool='query_warehouse')"
    assert error.message == "tool failed"
    assert error.context == {"tool": "query_warehouse", "attempt": 2}


def test_message_only_errors_render_plainly() -> None:
    assert str(CausiqError("something went wrong")) == "something went wrong"
