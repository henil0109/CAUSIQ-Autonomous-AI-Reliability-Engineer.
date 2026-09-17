"""A deterministic `ModelClient` for offline tests (invariant I6).

Maturity: hardened.

The fake plays back a fixed script, one entry per call to `investigate()`.
An entry is one of three things:

* a `ModelTurn` - returned exactly as given (the common case: "the model
  requests this tool call" or "the model concludes with this analysis").
* a callable `(history) -> ModelTurn` - given the conversation so far so it
  can build a response that *depends on* what actually happened, which is
  the only way a final analysis can cite real evidence ids: they are minted
  by the ledger at run time and cannot be known when the test is written.
  The callable inspects the `ToolResultBlock`s already in `history` to find
  them.
* an `Exception` instance - raised instead of returned, for scripting "the
  model refused" or "the model's response was malformed" deterministically.

Running out of script is a test-authoring bug, not a modeled failure mode, so
it raises a plain `RuntimeError` rather than a `causiq.errors` type - it must
never be silently absorbed by the agent's own error handling, which would
turn an under-scripted test into a quietly-wrong pass instead of a loud
failure pointing at the test itself.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from causiq.llm.ports import ConversationMessage, ModelTurn

ScriptEntry = ModelTurn | Callable[[tuple[ConversationMessage, ...]], ModelTurn] | Exception


@dataclass(frozen=True)
class CapturedCall:
    """What one `investigate()` call was given - for tests to assert on
    context assembly (Engineering Contract, P0.6 6) without needing a real
    model."""

    system: str
    tools: tuple[dict[str, JsonValue], ...]
    history: tuple[ConversationMessage, ...]


class FakeModelClient:
    """Deterministic, scripted `ModelClient` implementation.

    Satisfies `causiq.llm.ports.ModelClient` structurally - nothing declares
    inheritance, matching every other port in this codebase (Clock, Tool,
    AuditSink).
    """

    def __init__(self, script: Sequence[ScriptEntry]) -> None:
        self._script: list[ScriptEntry] = list(script)
        self.calls: list[CapturedCall] = []

    @property
    def calls_made(self) -> int:
        return len(self.calls)

    @property
    def remaining(self) -> int:
        """How many scripted entries are left - lets a test assert the whole
        script was consumed, catching an over-scripted test too."""
        return len(self._script)

    def investigate(
        self,
        *,
        system: str,
        tools: tuple[dict[str, JsonValue], ...],
        history: tuple[ConversationMessage, ...],
    ) -> ModelTurn:
        self.calls.append(CapturedCall(system=system, tools=tools, history=history))
        if not self._script:
            msg = (
                "FakeModelClient script exhausted: the agent requested another turn "
                f"after {len(self.calls)} call(s), but the test did not script one. "
                "This is a test-authoring bug, not a simulated model failure."
            )
            raise RuntimeError(msg)

        entry = self._script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        if callable(entry):
            return entry(history)
        return entry
