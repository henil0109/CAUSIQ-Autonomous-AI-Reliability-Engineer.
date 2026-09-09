"""Causiq error taxonomy.

Maturity: hardened.

Why this exists (Engineering Contract 8): an agent that fails at minute nine of a
run with an unclassified exception is undebuggable, and retry policy is undecidable.
Classifying every error by *recoverability* is what turns requirement 22 (failure
handling and recovery) from an aspiration into code.

The central distinction is `recoverable`:

* **recoverable** - the run continues. A denied or failed tool call becomes an
  `is_error` tool result the model can adapt to; that adaptation is the entire
  point of an investigating agent.
* **non-recoverable** - the run terminates in a recorded terminal state. It never
  crashes silently and never leaves a half-written audit journal.

Security note: `context` is rendered into log lines and error messages. Never put a
secret, credential, or raw API key in it.
"""

from __future__ import annotations

from typing import ClassVar


class CausiqError(Exception):
    """Base for every error Causiq raises deliberately.

    Anything that is *not* a `CausiqError` escaping our own code is a bug, not a
    handled condition.
    """

    #: Whether an investigation can continue after this error.
    recoverable: ClassVar[bool] = False

    def __init__(self, message: str, /, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, object] = dict(context)

    def __str__(self) -> str:
        if not self.context:
            return self.message
        rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(self.context.items()))
        return f"{self.message} ({rendered})"


# --------------------------------------------------------------------------- #
# Startup / configuration
# --------------------------------------------------------------------------- #
class ConfigurationError(CausiqError):
    """Invalid or missing configuration.

    Raised at process start only. Configuration is validated once, up front, so
    this can never surface mid-investigation.
    """


# --------------------------------------------------------------------------- #
# Domain invariant violations - always fatal to the run
# --------------------------------------------------------------------------- #
class DomainError(CausiqError):
    """A core invariant (Engineering Contract 2) was violated."""


class UnresolvedCitationError(DomainError):
    """An analysis cited evidence that is not in the run's ledger.

    This is the enforcement point for invariant I1 - no claim without evidence.
    An analysis that raises this is rejected and never shown to an operator.
    """


class LedgerIntegrityError(DomainError):
    """The evidence ledger was mutated, duplicated, or failed digest verification.

    Enforcement point for invariant I2 - evidence is immutable and attributable.
    """


# --------------------------------------------------------------------------- #
# Model interaction (adapters land in P0.6; the taxonomy is fixed now)
# --------------------------------------------------------------------------- #
class ModelError(CausiqError):
    """Base for failures originating from the model call."""


class ModelTransientError(ModelError):
    """429, 5xx, connection failure, or timeout. Retryable with backoff."""

    recoverable: ClassVar[bool] = True


class ModelRefusalError(ModelError):
    """The model declined the request (`stop_reason == "refusal"`).

    Terminal by design. The refusal category and explanation are recorded in the
    audit journal rather than retried, because retrying a policy decline is both
    futile and dishonest.
    """


class ModelContractError(ModelError):
    """The model returned output that does not satisfy the agreed schema.

    Permits exactly one repair attempt (Engineering Contract 8) before failing.
    """


# --------------------------------------------------------------------------- #
# Tool execution - recoverable, because adaptation is the point
# --------------------------------------------------------------------------- #
class ToolError(CausiqError):
    """Base for failures originating from a tool call."""

    recoverable: ClassVar[bool] = True


class ToolAuthorizationError(ToolError):
    """The acting identity was not authorized to invoke the tool.

    Enforcement point for invariant I4 - read is default, write is granted.
    Recoverable: a denial is data returned to the model, not a crash.
    """


class ToolExecutionError(ToolError):
    """The tool itself failed while executing."""


class ToolTimeoutError(ToolError):
    """The tool exceeded its execution timeout."""


# --------------------------------------------------------------------------- #
# Bounded execution
# --------------------------------------------------------------------------- #
class BudgetExceededError(CausiqError):
    """A run exhausted its turn, tool-call, token, or wall-clock budget.

    Enforcement point for invariant I5. Not recoverable *within* the run, but it
    is an orderly stop: the run ends INCONCLUSIVE with its partial ledger and
    journal intact (invariant I8).
    """


def all_error_types() -> tuple[type[CausiqError], ...]:
    """Every concrete error class in the taxonomy, depth-first, deterministic order.

    Used by the contract test that asserts the taxonomy is complete and that each
    class declares its recoverability deliberately.
    """

    def walk(cls: type[CausiqError]) -> list[type[CausiqError]]:
        found = [cls]
        for sub in sorted(cls.__subclasses__(), key=lambda c: c.__name__):
            found.extend(walk(sub))
        return found

    return tuple(walk(CausiqError))
