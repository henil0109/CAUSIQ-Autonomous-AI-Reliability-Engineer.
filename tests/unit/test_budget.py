"""Budget arithmetic and enforcement - invariant I5, "every run is bounded"."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from causiq.clock import FrozenClock
from causiq.domain import Budget, BudgetTracker
from causiq.errors import BudgetExceededError

pytestmark = pytest.mark.unit


@pytest.fixture
def tracker(budget: Budget, clock: FrozenClock) -> BudgetTracker:
    return BudgetTracker(budget, clock)


def test_turns_are_allowed_up_to_the_limit(tracker: BudgetTracker) -> None:
    for _ in range(tracker.budget.max_turns):
        tracker.begin_turn()
    assert tracker.usage().turns == 3


def test_the_turn_after_the_limit_is_refused(tracker: BudgetTracker) -> None:
    """The gate raises *before* the work, so the limit is a ceiling on spend."""
    for _ in range(tracker.budget.max_turns):
        tracker.begin_turn()
    with pytest.raises(BudgetExceededError) as caught:
        tracker.begin_turn()
    assert caught.value.context["limit"] == "max_turns"
    assert caught.value.context["allowed"] == 3
    assert tracker.usage().turns == 3  # the refused turn was not counted


def test_tool_calls_are_bounded(tracker: BudgetTracker) -> None:
    for _ in range(tracker.budget.max_tool_calls):
        tracker.begin_tool_call()
    with pytest.raises(BudgetExceededError, match="tool call budget"):
        tracker.begin_tool_call()
    assert tracker.usage().tool_calls == 5


def test_tokens_accumulate_and_are_bounded(tracker: BudgetTracker) -> None:
    tracker.record_tokens(input_tokens=400, output_tokens=100)
    assert tracker.usage().total_tokens == 500
    tracker.record_tokens(input_tokens=400, output_tokens=100)
    assert tracker.usage().total_tokens == 1000  # exactly at the limit is fine
    with pytest.raises(BudgetExceededError) as caught:
        tracker.record_tokens(input_tokens=1, output_tokens=0)
    assert caught.value.context["limit"] == "max_total_tokens"


def test_token_counts_must_be_non_negative(tracker: BudgetTracker) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        tracker.record_tokens(input_tokens=-1, output_tokens=0)
    with pytest.raises(ValueError, match="non-negative"):
        tracker.record_tokens(input_tokens=0, output_tokens=-5)


def test_deadline_is_enforced_from_monotonic_time(
    budget: Budget,
    clock: FrozenClock,
) -> None:
    """Monotonic, not wall-clock: a system clock change must not move a deadline."""
    tracker = BudgetTracker(budget, clock)
    clock.advance(timedelta(seconds=59))
    tracker.check_deadline()  # still inside
    clock.advance(timedelta(seconds=1))
    with pytest.raises(BudgetExceededError) as caught:
        tracker.check_deadline()
    assert caught.value.context["limit"] == "deadline_seconds"


def test_deadline_blocks_a_new_turn(budget: Budget, clock: FrozenClock) -> None:
    tracker = BudgetTracker(budget, clock)
    clock.advance(timedelta(seconds=120))
    with pytest.raises(BudgetExceededError, match="deadline"):
        tracker.begin_turn()
    with pytest.raises(BudgetExceededError, match="deadline"):
        tracker.begin_tool_call()


def test_elapsed_seconds_tracks_the_clock(budget: Budget, clock: FrozenClock) -> None:
    tracker = BudgetTracker(budget, clock)
    assert tracker.elapsed_seconds == 0.0
    clock.advance(timedelta(seconds=12.5))
    assert tracker.elapsed_seconds == pytest.approx(12.5)
    assert tracker.usage().elapsed_seconds == pytest.approx(12.5)


def test_budget_exhaustion_is_not_recoverable_within_the_run() -> None:
    """It ends the run - but cleanly, as BUDGET_EXCEEDED (invariant I8)."""
    assert BudgetExceededError.recoverable is False


@pytest.mark.parametrize(
    "field",
    ["max_turns", "max_tool_calls", "max_total_tokens", "deadline_seconds"],
)
def test_budget_limits_must_be_positive(field: str) -> None:
    valid: dict[str, float] = {
        "max_turns": 1,
        "max_tool_calls": 1,
        "max_total_tokens": 1,
        "deadline_seconds": 1.0,
    }
    with pytest.raises(ValidationError):
        Budget(**{**valid, field: 0})  # type: ignore[arg-type]
