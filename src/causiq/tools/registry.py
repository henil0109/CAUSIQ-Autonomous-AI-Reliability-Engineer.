"""The tool registry: the closed set of things the model may ask for.

Maturity: hardened.

The registry answers one question - "is this name a tool, and which one?" - and it
answers it from a set fixed before the run begins. That is what makes invariant I3
enforceable: a name the registry does not know never reaches execution, and there
is no path from a model-supplied string to an arbitrary Python callable.

Two properties are load-bearing:

* **Duplicate names are rejected at registration.** A second `query_warehouse`
  silently shadowing the first would mean the authorization decision was made
  against one tool and the execution against another. That is a security bug, so
  it fails loudly at start-up rather than resolving by luck of ordering.
* **Emission is name-sorted and byte-stable.** Tools are rendered before the
  system prompt and the messages, so any instability in this output invalidates
  the entire prompt cache for every subsequent turn (Engineering Contract 6.3).
  Registration order must not be observable in the output, and it is not.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from causiq.errors import ToolRegistrationError, UnknownToolError
from causiq.tools.base import Tool, ToolInput, schema_digest, tool_schema


class ToolRegistry:
    """An immutable-after-setup collection of the tools a run may use."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # -- registration ------------------------------------------------------- #

    def register(self, tool: Tool) -> None:
        """Add a tool, validating everything that must be true of it.

        Every check here is a start-up check by design (invariant I5's sibling
        concern): the tool surface is frozen before a run begins, so none of these
        failures can surprise a run in progress.
        """
        spec = tool.spec
        spec.validate_name()

        if spec.name in self._tools:
            msg = "a tool with this name is already registered"
            raise ToolRegistrationError(msg, name=spec.name)

        # Widened to `object` deliberately: the declared type says this is a
        # ToolInput subclass, but `register` is a trust boundary and a
        # duck-typed tool can hand us anything at runtime.
        declared: object = tool.input_model
        if not isinstance(declared, type) or not issubclass(declared, ToolInput):
            msg = "tool input_model must subclass causiq.tools.base.ToolInput"
            raise ToolRegistrationError(msg, name=spec.name, input_model=repr(declared))

        # extra="forbid" is what produces `additionalProperties: false`. Without
        # it the schema is not strict-eligible and the API would reject it.
        if tool.input_model.model_config.get("extra") != "forbid":
            msg = 'tool input_model must set extra="forbid" so its schema is strict-eligible'
            raise ToolRegistrationError(msg, name=spec.name)

        self._tools[spec.name] = tool

    def register_all(self, tools: tuple[Tool, ...]) -> None:
        """Register several tools. Fails on the first problem, registering none after it."""
        for tool in tools:
            self.register(tool)

    # -- lookup ------------------------------------------------------------- #

    def get(self, name: str) -> Tool | None:
        """The tool with this name, or None. Never raises for a miss."""
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        """The tool with this name, or raise `UnknownToolError`.

        The error carries the available names, because the executor puts them in
        the result the model sees - telling it what it *could* have called is what
        lets it recover rather than guess again.
        """
        tool = self._tools.get(name)
        if tool is None:
            msg = "no tool is registered under this name"
            raise UnknownToolError(msg, name=name, available=list(self.names()))
        return tool

    def names(self) -> tuple[str, ...]:
        """Registered names, sorted. Sorted, not insertion-ordered, on purpose."""
        return tuple(sorted(self._tools))

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        """Iterate in name order, so callers inherit determinism for free."""
        return (self._tools[name] for name in self.names())

    # -- schema emission ---------------------------------------------------- #

    def schemas(self) -> tuple[dict[str, Any], ...]:
        """API tool definitions, name-sorted and byte-stable.

        This is what goes into the `tools` field of a request, and therefore into
        the front of the cached prefix.
        """
        return tuple(tool_schema(tool) for tool in self)

    def schema_digest(self) -> str:
        """SHA-256 of the emitted tool list.

        Asserted equal across calls in the contract tests. If this digest ever
        changes mid-run, the prompt cache is silently gone and every turn is
        paying full input price - a cost regression with no error message.
        """
        return schema_digest(self.schemas())
