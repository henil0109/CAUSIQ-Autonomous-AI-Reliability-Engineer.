"""The ModelClient port and the provider-neutral conversation vocabulary.

Maturity: hardened.

Why this exists (ADR-0001, ADR-0004): the investigating agent must depend on an
interface, never on the Anthropic SDK directly. Everything in this module is
plain, JSON-shaped data - no Anthropic type appears here, and none of it
requires the `anthropic` package to be installed to import this file.

The interface is deliberately small - one method. An investigation turn can
only end one of two ways: the model asks for tool calls, or it concludes with a
structured `Analysis`. `ModelTurn` enforces that as a closed, mutually
exclusive pair; a response that is neither must never be squeezed into a
`ModelTurn` at all - the client raises `causiq.errors.ModelContractError`
instead (see `causiq.llm.anthropic_client` and `causiq.llm.fake_client`).

Context engineering (Engineering Contract, P0.6): `ConversationMessage` and its
content blocks are the *entire* surface the model ever sees of an
investigation's internal state. There is no ambient access to the ledger, the
tool registry, or any other application object - only whatever text an earlier
turn explicitly put into a `TextBlock` or `ToolResultBlock`. This is what makes
"the model never touches a system directly" (invariant I3) hold at the
conversation layer, not just at the tool-execution layer.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from causiq.domain import Analysis


class MessageRole(StrEnum):
    """Who authored a turn in the conversation."""

    USER = "user"
    ASSISTANT = "assistant"


class TextBlock(BaseModel):
    """Plain text - the incident brief, or a model's own prose."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["text"] = "text"
    text: str = Field(min_length=1)


class ToolCallBlock(BaseModel):
    """One tool call, either requested by the model or echoed back into its
    own turn's history afterward.

    `arguments` is untrusted (Engineering Contract 7.4/13): it is exactly what
    the model produced, validated later by the tool's own input schema inside
    `causiq.tools.executor.ToolExecutor` - never trusted or interpreted here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["tool_call"] = "tool_call"
    call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    """One tool's outcome, as reported back to the model.

    `content` is `causiq.tools.executor.ToolResult.content` verbatim - already
    bounded and already carrying the evidence id header on success. Nothing
    here re-derives or re-summarizes it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["tool_result"] = "tool_result"
    call_id: str = Field(min_length=1)
    content: str
    is_error: bool = False


#: Discriminated on the literal `type` field each block carries, so pydantic
#: resolves the union deterministically rather than by best-effort field
#: matching. The `Annotated` wrapper is required here rather than on the
#: `tuple` field that uses it - pydantic discriminates the union itself, not a
#: container of it.
ContentBlock = Annotated[TextBlock | ToolCallBlock | ToolResultBlock, Field(discriminator="type")]


class ConversationMessage(BaseModel):
    """One turn of the investigation conversation. Immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: MessageRole
    content: tuple[ContentBlock, ...] = Field(min_length=1)


class ModelTurn(BaseModel):
    """What the model decided this turn - exactly one of two things.

    Never both, never neither. A client that cannot classify its response as
    one of these must raise `ModelContractError` rather than construct a
    degenerate `ModelTurn` - that is what keeps this type a safe thing for the
    agent loop to pattern-match on without a third "I don't know" case to
    forget to handle.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_calls: tuple[ToolCallBlock, ...] = ()
    analysis: Analysis | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _exactly_one_kind(self) -> ModelTurn:
        has_tool_calls = bool(self.tool_calls)
        has_analysis = self.analysis is not None
        if has_tool_calls == has_analysis:
            msg = "a ModelTurn must carry either tool_calls or an analysis, never both or neither"
            raise ValueError(msg)
        return self

    @property
    def is_final(self) -> bool:
        """Whether this turn concludes the investigation."""
        return self.analysis is not None


@runtime_checkable
class ModelClient(Protocol):
    """Port: the model that drives one investigation turn (ADR-0001).

    Every call is complete and stateless - the full system prompt, every tool
    schema, and the entire conversation history are sent each time, matching
    the underlying API's own statelessness (Engineering Contract 6). Nothing
    about this signature is Anthropic-specific: `tools` is plain JSON data
    (`causiq.tools.ToolRegistry.schemas()`), and `history` is built entirely
    from the types in this module.

    Raises (from `causiq.errors`): `ModelTransientError` (retryable, e.g. rate
    limits), `ModelRefusalError` (the model declined), `ModelContractError`
    (the response could not be interpreted as a valid turn).
    """

    def investigate(
        self,
        *,
        system: str,
        tools: tuple[dict[str, JsonValue], ...],
        history: tuple[ConversationMessage, ...],
    ) -> ModelTurn:
        """One request/response cycle. Never mutates `history` in place."""
        ...
