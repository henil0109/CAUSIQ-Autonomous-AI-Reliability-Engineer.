"""Bounded execution - invariant I5.

Maturity: hardened.

Why this exists: an agent with tools and no ceiling is an unbounded spend and an
unbounded blast radius. Every run declares its limits up front, the limits are
checked before each turn and each tool call, and exhausting them is an orderly
stop (`BudgetExceededError` -> `RunState.BUDGET_EXCEEDED`) rather than a crash.

`Budget` is the frozen declaration; `BudgetTracker` is the runtime enforcer. They
are separate so the budget can be persisted on the run record and compared across
runs, while the counters stay a runtime concern.

Deadlines use monotonic time, never wall-clock - a system clock adjustment must
not extend or truncate a run.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from causiq.clock import Clock
from causiq.errors import BudgetExceededError


class Budget(BaseModel):
    """The declared ceiling for one investigation run. Immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_turns: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_total_tokens: int = Field(gt=0)
    deadline_seconds: float = Field(gt=0)


class BudgetUsage(BaseModel):
    """What a run actually consumed. Immutable snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    turns: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0.0)

    @property
    def total_tokens(self) -> int:
        """Tokens consumed in both directions."""
        return self.input_tokens + self.output_tokens


class BudgetTracker:
    """Runtime enforcement of a `Budget`.

    Every `begin_*` method is a gate: it raises *before* the work happens, so the
    limit is a ceiling on what is spent rather than a report of what was
    overspent.
    """

    def __init__(self, budget: Budget, clock: Clock) -> None:
        self._budget = budget
        self._clock = clock
        self._started_monotonic = clock.monotonic()
        self._turns = 0
        self._tool_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0

    @property
    def budget(self) -> Budget:
        return self._budget

    @property
    def elapsed_seconds(self) -> float:
        return self._clock.monotonic() - self._started_monotonic

    def usage(self) -> BudgetUsage:
        """An immutable snapshot of consumption so far."""
        return BudgetUsage(
            turns=self._turns,
            tool_calls=self._tool_calls,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            elapsed_seconds=self.elapsed_seconds,
        )

    def check_deadline(self) -> None:
        """Raise if the wall-clock deadline has passed."""
        elapsed = self.elapsed_seconds
        if elapsed >= self._budget.deadline_seconds:
            msg = "run deadline exceeded"
            raise BudgetExceededError(
                msg,
                limit="deadline_seconds",
                allowed=self._budget.deadline_seconds,
                elapsed=elapsed,
            )

    def begin_turn(self) -> None:
        """Gate a model turn. Raises if no turn budget remains."""
        self.check_deadline()
        if self._turns >= self._budget.max_turns:
            msg = "turn budget exhausted"
            raise BudgetExceededError(
                msg, limit="max_turns", allowed=self._budget.max_turns, used=self._turns
            )
        self._turns += 1

    def begin_tool_call(self) -> None:
        """Gate a tool call. Raises if no tool-call budget remains."""
        self.check_deadline()
        if self._tool_calls >= self._budget.max_tool_calls:
            msg = "tool call budget exhausted"
            raise BudgetExceededError(
                msg,
                limit="max_tool_calls",
                allowed=self._budget.max_tool_calls,
                used=self._tool_calls,
            )
        self._tool_calls += 1

    def record_tokens(self, *, input_tokens: int, output_tokens: int) -> None:
        """Account for tokens a completed call consumed.

        Recorded after the fact - the API reports usage only once the call
        returns - so this raises when the *cumulative* total passes the ceiling,
        stopping the next turn rather than the one already paid for.
        """
        if input_tokens < 0 or output_tokens < 0:
            msg = "token counts must be non-negative"
            raise ValueError(msg)
        self._input_tokens += input_tokens
        self._output_tokens += output_tokens
        total = self._input_tokens + self._output_tokens
        if total > self._budget.max_total_tokens:
            msg = "token budget exhausted"
            raise BudgetExceededError(
                msg, limit="max_total_tokens", allowed=self._budget.max_total_tokens, used=total
            )
