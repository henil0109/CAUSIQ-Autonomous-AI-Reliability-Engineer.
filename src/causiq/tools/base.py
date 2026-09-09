"""The tool contract: what a tool is, what it declares, and what it returns.

Maturity: hardened.

A tool is the *only* way Causiq touches an external system (invariant I3). The
model never calls a Python function; it emits a `ToolRequest` naming a registered
tool, and the executor decides whether that request becomes a call.

Three things every tool must declare, and why each is mandatory rather than
optional:

* **A capability and a mutation flag** (`ToolSpec`). Without them the executor
  cannot enforce invariant I4, because it would have nothing to check. Defaulting
  `mutating` to `False` is what makes "read is default" true by construction - a
  tool author has to *opt in* to being dangerous.
* **A typed argument model** (`input_model`). It produces the JSON Schema the API
  constrains generation against, and it is what the executor validates against
  before execution. One declaration, two enforcement points.
* **A reason for every call** (`ToolInput.reason`). Inherited by every tool's
  argument model, so the model must state why it is asking before it asks. That
  lands in the audit trail and in the evidence record, and it becomes directly
  gradeable by the Phase 4 tool-use evaluator - one required field for a real
  explainability signal.

Schema emission is deliberately deterministic. Render order is `tools` -> `system`
-> `messages`, so a tool list that serialises differently between turns silently
invalidates the whole prompt cache (Engineering Contract 6.3). `canonical_json`
and the registry's name-sorted emission are what make that byte-stable.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from causiq.authz import Permission
from causiq.domain.enums import EvidenceSource, RiskLevel
from causiq.errors import ToolRegistrationError

#: Tool names are snake_case and bounded. The API accepts a wider character set;
#: we are stricter so names read consistently in prompts and audit journals.
TOOL_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


def canonical_json(payload: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace.

    The prompt cache is a prefix match, so two logically identical tool lists that
    serialise to different bytes cost a full cache miss. This is the function that
    stops that happening.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


class ToolInput(BaseModel):
    """Base class for every tool's argument model.

    `extra="forbid"` is not stylistic: it is what makes pydantic emit
    `additionalProperties: false`, which the API requires for a strict tool schema.
    `frozen=True` means a validated payload cannot be altered between validation
    and execution - the thing that was checked is the thing that runs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "Why this call is being made, in one sentence. Recorded as evidence provenance."
        ),
    )


class ToolOutput(BaseModel):
    """What a tool returns on success.

    Content is an opaque string; the tool decides its encoding. The executor does
    not interpret it - it records it verbatim as evidence, so that any claim built
    on it can be re-checked against exactly what the tool returned.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    content_type: str = "application/json"
    #: True when the tool hit a row or byte cap. Surfaced to the model so it knows
    #: it is reasoning about a partial answer.
    truncated: bool = False


class ToolSpec(BaseModel):
    """Static, immutable declaration of a tool's identity and security class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    #: Shown to the model. This is prompt text, so it is part of the cached prefix.
    description: str = Field(min_length=1)
    #: The capability an identity must hold to invoke this tool.
    permission: Permission
    #: Whether invoking this tool changes state. False by default - invariant I4.
    mutating: bool = False
    risk: RiskLevel = RiskLevel.LOW
    #: Which evidence source this tool's results are attributed to.
    source: EvidenceSource
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=600.0)

    def validate_name(self) -> None:
        """Raise if the name is not a well-formed tool name."""
        if not TOOL_NAME_PATTERN.match(self.name):
            msg = "tool name must be snake_case, 3-64 characters, starting with a letter"
            raise ToolRegistrationError(msg, name=self.name)


@runtime_checkable
class Tool(Protocol):
    """The interface every tool implements.

    A Protocol rather than a base class: tools are supplied by whatever module
    owns the system being queried, and they should not have to inherit from the
    capability layer to be usable by it.
    """

    @property
    def spec(self) -> ToolSpec:
        """Static metadata. Must be constant for the life of the process."""
        ...

    @property
    def input_model(self) -> type[ToolInput]:
        """The argument model. Must subclass `ToolInput`."""
        ...

    def run(self, payload: ToolInput) -> ToolOutput:
        """Execute against the real system.

        Receives an already-validated, already-authorized payload. A tool must
        never re-check authorization - that is the executor's job, and duplicating
        it invites the two checks to disagree.

        Raise any exception to signal failure; the executor classifies it,
        records it, and returns an error result to the model.
        """
        ...


def tool_schema(tool: Tool) -> dict[str, Any]:
    """The API tool definition for `tool`.

    `strict: True` plus `additionalProperties: false` plus a complete `required`
    list means arguments are schema-valid by construction, so the executor's own
    validation is a second line of defence rather than the primary one.
    """
    return {
        "name": tool.spec.name,
        "description": tool.spec.description,
        "input_schema": tool.input_model.model_json_schema(),
        "strict": True,
    }


def schema_digest(schemas: tuple[dict[str, Any], ...]) -> str:
    """SHA-256 over the canonical form of a tool list.

    Used to assert prompt-cache stability: if this digest changes between turns,
    the cached prefix is gone and every turn is paying full price.
    """
    return hashlib.sha256(canonical_json(list(schemas)).encode("utf-8")).hexdigest()
