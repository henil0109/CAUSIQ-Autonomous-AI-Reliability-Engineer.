"""The Anthropic adapter behind `ModelClient` (ADR-0001).

Maturity: hardened for the request/response translation logic, which is
exercised offline against a duck-typed fake client and never touches the
network in the default suite. NOT exercised against the live API by the
default suite - see the `live`-marked tests in
`tests/integration/test_anthropic_live.py`, skipped unless `ANTHROPIC_API_KEY`
is set. Every field name and parameter shape used below (`output_config`,
`thinking`, the exact `StopReason` values, `ToolUseBlock.id`/`.name`/`.input`,
`Usage.input_tokens`/`.output_tokens`) was checked directly against the
installed `anthropic` 1.6.0 package before this file was written, not recalled
from memory - guessing SDK shapes is exactly the failure mode Engineering
Contract 6 exists to prevent.

This is the *only* module in the project that imports `anthropic`. Every other
module - the agent, the domain model, every test that is not specifically
testing this file - depends on `causiq.llm.ports.ModelClient` and never sees
an Anthropic SDK type. That isolation is what makes "the interface is
provider-agnostic" a checked property rather than a claim: `grep -r anthropic
src/` outside this file returns nothing.

Credentials (Engineering Contract 7.5): this adapter never resolves
credentials implicitly. `from_settings()` reads exactly the
`ANTHROPIC_API_KEY` environment variable, via the existing
`causiq.config.Settings`, and raises `ConfigurationError` - clearly, at
construction time, before any network call - if it is absent. There is no
fallback to a CLI profile, a Claude web session, or any other ambient
credential source: this project's only supported credential path is that one
environment variable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import anthropic
from pydantic import JsonValue

from causiq.config import Settings
from causiq.domain import Analysis
from causiq.errors import (
    ConfigurationError,
    ModelContractError,
    ModelError,
    ModelRefusalError,
    ModelTransientError,
)
from causiq.llm.ports import (
    ConversationMessage,
    ModelTurn,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The model policy from Engineering Contract 6.1 - configuration, not a
#: literal scattered across call sites.
DEFAULT_MODEL_ID = "claude-opus-5"
DEFAULT_EFFORT = "high"
#: Non-streaming default (Engineering Contract 6.2). A call that genuinely
#: needs more would have to move to streaming - out of scope for P0.6.
DEFAULT_MAX_TOKENS = 16_000

#: Computed once at import time, not per call - `Analysis`'s schema does not
#: change during a process's lifetime.
_ANALYSIS_SCHEMA = Analysis.model_json_schema()

#: `StopReason` values (verified against `anthropic.types.stop_reason`) that
#: mean "the response is incomplete" rather than "the model refused" or
#: "the model wants a tool" - both treated as a contract violation, since
#: neither can be turned into a well-formed `ModelTurn`.
_INCOMPLETE_STOP_REASONS = frozenset({"max_tokens", "model_context_window_exceeded"})


@runtime_checkable
class _MessagesAPI(Protocol):
    """The one method this adapter calls on `client.messages`."""

    def create(self, **kwargs: Any) -> Any: ...


@runtime_checkable
class _AnthropicLike(Protocol):
    """The minimal surface this adapter needs from an Anthropic client.

    A Protocol, not a direct `anthropic.Anthropic` type hint, so tests can
    substitute a duck-typed fake with no key and no network - the same seam
    every other adapter in this codebase uses (ADR-0004).

    `messages` is declared as a read-only property, not a plain attribute:
    the real `anthropic.Anthropic.messages` is exposed that way, and a plain
    attribute annotation would require it to be settable too, which it is
    not - a mismatch mypy correctly flags.
    """

    @property
    def messages(self) -> _MessagesAPI: ...


def _build_request_messages(history: tuple[ConversationMessage, ...]) -> list[dict[str, Any]]:
    """Translate provider-neutral history into Anthropic's `messages` array.

    Plain dicts, matching the SDK's own documented usage - no typed `*Param`
    classes are needed to construct a request.
    """
    messages: list[dict[str, Any]] = []
    for turn in history:
        blocks: list[dict[str, Any]] = []
        for block in turn.content:
            if isinstance(block, TextBlock):
                blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolCallBlock):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.call_id,
                        "name": block.tool_name,
                        "input": block.arguments,
                    }
                )
            elif isinstance(block, ToolResultBlock):
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.call_id,
                        "content": block.content,
                        "is_error": block.is_error,
                    }
                )
        messages.append({"role": turn.role.value, "content": blocks})
    return messages


def _parse_response(response: Any) -> ModelTurn:
    """Translate an Anthropic `Message` into a provider-neutral `ModelTurn`.

    Raises `ModelRefusalError` or `ModelContractError` rather than ever
    returning a `ModelTurn` that could not be fully classified - see the
    module docstring on why guessing here is exactly what this file must not
    do.
    """
    usage = response.usage
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    stop_reason = response.stop_reason

    if stop_reason == "refusal":
        details = response.stop_details
        category = getattr(details, "category", None)
        explanation = getattr(details, "explanation", None)
        msg = "the model refused the request"
        raise ModelRefusalError(msg, category=str(category), explanation=str(explanation))

    if stop_reason == "tool_use":
        calls = tuple(
            ToolCallBlock(call_id=block.id, tool_name=block.name, arguments=dict(block.input))
            for block in response.content
            if block.type == "tool_use"
        )
        if not calls:
            msg = "stop_reason was tool_use but the response contained no tool_use block"
            raise ModelContractError(msg, stop_reason=stop_reason)
        return ModelTurn(tool_calls=calls, input_tokens=input_tokens, output_tokens=output_tokens)

    if stop_reason == "end_turn":
        text_blocks = [block.text for block in response.content if block.type == "text"]
        if not text_blocks:
            msg = "stop_reason was end_turn but the response contained no text"
            raise ModelContractError(msg, stop_reason=stop_reason)
        try:
            analysis = Analysis.model_validate_json(text_blocks[0])
        except Exception as exc:
            msg = "the model's final response did not satisfy the Analysis schema"
            raise ModelContractError(
                msg, stop_reason=stop_reason, parse_error=f"{type(exc).__name__}: {exc}"
            ) from exc
        return ModelTurn(analysis=analysis, input_tokens=input_tokens, output_tokens=output_tokens)

    if stop_reason in _INCOMPLETE_STOP_REASONS:
        msg = f"the model's response was incomplete ({stop_reason})"
        raise ModelContractError(msg, stop_reason=str(stop_reason))

    msg = f"unsupported stop_reason: {stop_reason!r}"
    raise ModelContractError(msg, stop_reason=str(stop_reason))


class AnthropicModelClient:
    """`ModelClient` backed by the real Anthropic API.

    Maturity: hardened for translation; not production-ready as a whole -
    there is no retry policy beyond the SDK's own, and no live-traffic
    experience. See Engineering Contract 11 for what production-ready
    requires.

    `client` accepts a real `anthropic.Anthropic` *or* anything matching
    `_AnthropicLike` - the union, rather than the bare protocol, is what lets
    `from_settings()` pass a real client without a structural-typing mismatch
    (the SDK's actual `create` overloads do not satisfy a `**kwargs`-shaped
    Protocol method under mypy, a known limitation, not a real incompatibility)
    while tests still substitute an offline duck-typed fake.
    """

    def __init__(
        self,
        *,
        client: anthropic.Anthropic | _AnthropicLike,
        model: str = DEFAULT_MODEL_ID,
        effort: str = DEFAULT_EFFORT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens

    @classmethod
    def from_settings(cls, settings: Settings) -> AnthropicModelClient:
        """Build a real, network-backed client from `ANTHROPIC_API_KEY`.

        Fails clearly and immediately - never lazily on the first call, and
        never by resolving a credential from anywhere else - if the key is
        absent. This is the project's only supported credential path
        (Engineering Contract 7.5): no CLI profile, no Claude web session, no
        other ambient source is ever consulted.
        """
        if not settings.has_api_key:
            msg = (
                "ANTHROPIC_API_KEY is not set. The Anthropic adapter requires it "
                "explicitly - Causiq never falls back to a CLI profile, a Claude "
                "web session, or any other ambient credential source."
            )
            raise ConfigurationError(msg)
        secret = settings.anthropic_api_key
        assert secret is not None  # guaranteed by has_api_key above
        client = anthropic.Anthropic(api_key=secret.get_secret_value())
        return cls(
            client=client,
            model=settings.model_id,
            effort=settings.model_effort,
            max_tokens=settings.model_max_tokens,
        )

    def investigate(
        self,
        *,
        system: str,
        tools: tuple[dict[str, JsonValue], ...],
        history: tuple[ConversationMessage, ...],
    ) -> ModelTurn:
        response = self._create(system=system, tools=tools, history=history)
        return _parse_response(response)

    def _create(
        self,
        *,
        system: str,
        tools: tuple[dict[str, JsonValue], ...],
        history: tuple[ConversationMessage, ...],
    ) -> Any:
        """The one network call, with the SDK's typed exceptions mapped onto
        the causiq error taxonomy - a most-specific-first chain (Engineering
        Contract 8), verified against the real hierarchy: `RateLimitError`
        and `InternalServerError` are `APIStatusError` subclasses;
        `APIConnectionError` (and its subclass `APITimeoutError`) is a
        separate hierarchy entirely.
        """
        try:
            # Plain dicts, matching the SDK's own documented usage rather than
            # its typed `*Param` TypedDicts - correct at runtime (exercised by
            # the offline adapter tests against a duck-typed client), but the
            # real `Messages.create` overloads are typed against those exact
            # TypedDicts, which a plain `dict[str, ...]` literal cannot satisfy
            # structurally. This is the one call site where that gap is
            # unavoidable given `_client`'s dual real-or-fake type; see the
            # class docstring for why that duality exists.
            return self._client.messages.create(  # type: ignore[call-overload]
                model=self._model,
                max_tokens=self._max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=list(tools),
                messages=_build_request_messages(history),
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": _ANALYSIS_SCHEMA},
                },
            )
        except anthropic.RateLimitError as exc:
            retry_after = exc.response.headers.get("retry-after")
            msg = "rate limited by the Anthropic API"
            raise ModelTransientError(msg, retry_after=retry_after) from exc
        except anthropic.APIConnectionError as exc:
            msg = f"connection error calling the Anthropic API: {exc}"
            raise ModelTransientError(msg) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                msg = f"Anthropic API server error ({exc.status_code})"
                raise ModelTransientError(msg, status_code=exc.status_code) from exc
            msg = f"Anthropic API error ({exc.status_code}): {exc.message}"
            raise ModelError(msg, status_code=exc.status_code) from exc


__all__: Sequence[str] = ("AnthropicModelClient",)
