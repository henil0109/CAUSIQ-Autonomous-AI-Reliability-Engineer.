"""`AnthropicModelClient` - request/response translation, offline.

Every test here uses a duck-typed fake in place of `anthropic.Anthropic` -
never the real network, never a real key. What is verified is the adapter's
*own* logic: how it builds a request from `ConversationMessage` history, and
how it classifies a response's `stop_reason` into a `ModelTurn` or a typed
error. See `tests/integration/test_anthropic_live.py` for the (opt-in, key-
gated) tests that exercise the real API.

The fake response/exception objects below duck-type only the handful of
attributes `causiq.llm.anthropic_client` actually reads - confirmed against
the installed `anthropic` 1.6.0 package's real type stubs before this file
was written (`TextBlock.text`, `ToolUseBlock.id`/`.name`/`.input`,
`Usage.input_tokens`/`.output_tokens`, `Message.stop_reason`/`.content`/
`.usage`/`.stop_details`), so they are faithful stand-ins, not guesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import JsonValue

from causiq.config import load_settings
from causiq.domain import Analysis, AnalysisOutcome
from causiq.errors import (
    ConfigurationError,
    ModelContractError,
    ModelError,
    ModelRefusalError,
    ModelTransientError,
)
from causiq.llm.anthropic_client import AnthropicModelClient
from causiq.llm.ports import (
    ConversationMessage,
    MessageRole,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)

pytestmark = pytest.mark.unit

HISTORY = (ConversationMessage(role=MessageRole.USER, content=(TextBlock(text="hi"),)),)


# --------------------------------------------------------------------------- #
# Duck-typed fakes for the Anthropic SDK surface this adapter reads
# --------------------------------------------------------------------------- #
@dataclass
class FakeUsage:
    input_tokens: int = 10
    output_tokens: int = 5
    #: `Optional[int]` on the real `Usage` type - `None` is the common case
    #: (nothing cache-related to report), matched here so the adapter's `or 0`
    #: normalization is exercised by the *existing* tests, not just new ones.
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


@dataclass
class FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeStopDetails:
    category: str = "cyber"
    explanation: str = "unsafe request"


@dataclass
class FakeMessage:
    stop_reason: str
    content: list[Any] = field(default_factory=list)
    usage: FakeUsage = field(default_factory=FakeUsage)
    stop_details: FakeStopDetails | None = None


class FakeMessagesAPI:
    def __init__(self, response: FakeMessage | None = None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> FakeMessage:
        self.last_kwargs = kwargs
        if self._exc is not None:
            raise self._exc
        assert self._response is not None
        return self._response


class FakeAnthropicSDK:
    def __init__(self, response: FakeMessage | None = None, exc: Exception | None = None) -> None:
        self.messages = FakeMessagesAPI(response, exc)


def _client(
    response: FakeMessage | None = None, exc: Exception | None = None
) -> tuple[AnthropicModelClient, FakeAnthropicSDK]:
    sdk = FakeAnthropicSDK(response, exc)
    return AnthropicModelClient(client=sdk, model="claude-opus-5", effort="high"), sdk


# --------------------------------------------------------------------------- #
# Credentials (Engineering Contract 7.5)
# --------------------------------------------------------------------------- #
def test_from_settings_fails_clearly_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY is not set"):
        AnthropicModelClient.from_settings(load_settings())


def test_from_settings_never_falls_back_to_another_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure message itself asserts the no-fallback policy, so a future
    edit that adds an implicit credential source has to consciously break
    this test, not silently slip past it."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigurationError) as caught:
        AnthropicModelClient.from_settings(load_settings())
    assert "CLI profile" in str(caught.value)
    assert "Claude web session" in str(caught.value)


def test_from_settings_succeeds_with_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key-000000000000")
    client = AnthropicModelClient.from_settings(load_settings())
    assert isinstance(client, AnthropicModelClient)


# --------------------------------------------------------------------------- #
# Request construction
# --------------------------------------------------------------------------- #
def test_request_carries_the_model_policy() -> None:
    resp = FakeMessage("end_turn", [FakeTextBlock(_analysis_json())])
    client, sdk = _client(resp)
    client.investigate(system="be honest", tools=(), history=HISTORY)

    assert sdk.messages.last_kwargs is not None
    kwargs = sdk.messages.last_kwargs
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert kwargs["output_config"]["effort"] == "high"
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert kwargs["output_config"]["format"]["schema"] == Analysis.model_json_schema()


def test_system_prompt_is_sent_with_a_cache_breakpoint() -> None:
    resp = FakeMessage("end_turn", [FakeTextBlock(_analysis_json())])
    client, sdk = _client(resp)
    client.investigate(system="be honest", tools=(), history=HISTORY)

    assert sdk.messages.last_kwargs is not None
    system = sdk.messages.last_kwargs["system"]
    assert system == [{"type": "text", "text": "be honest", "cache_control": {"type": "ephemeral"}}]


def test_tools_are_forwarded_verbatim() -> None:
    resp = FakeMessage("end_turn", [FakeTextBlock(_analysis_json())])
    client, sdk = _client(resp)
    tools: tuple[dict[str, JsonValue], ...] = ({"name": "query_warehouse", "input_schema": {}},)
    client.investigate(system="s", tools=tools, history=HISTORY)
    assert sdk.messages.last_kwargs is not None
    assert sdk.messages.last_kwargs["tools"] == list(tools)


def test_history_translates_every_block_kind() -> None:
    history = (
        ConversationMessage(role=MessageRole.USER, content=(TextBlock(text="incident"),)),
        ConversationMessage(
            role=MessageRole.ASSISTANT,
            content=(
                ToolCallBlock(
                    call_id="c1", tool_name="query_warehouse", arguments={"sql": "SELECT 1"}
                ),
            ),
        ),
        ConversationMessage(
            role=MessageRole.USER,
            content=(ToolResultBlock(call_id="c1", content="evidence_id: ev_001", is_error=False),),
        ),
    )
    resp = FakeMessage("end_turn", [FakeTextBlock(_analysis_json())])
    client, sdk = _client(resp)
    client.investigate(system="s", tools=(), history=history)

    assert sdk.messages.last_kwargs is not None
    sent = sdk.messages.last_kwargs["messages"]
    assert sent[0] == {"role": "user", "content": [{"type": "text", "text": "incident"}]}
    assert sent[1] == {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "c1",
                "name": "query_warehouse",
                "input": {"sql": "SELECT 1"},
            }
        ],
    }
    assert sent[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "c1",
                "content": "evidence_id: ev_001",
                "is_error": False,
            }
        ],
    }


# --------------------------------------------------------------------------- #
# Response classification
# --------------------------------------------------------------------------- #
def _analysis_json() -> str:
    return (
        '{"outcome":"inconclusive","summary":"s","confidence":"low",'
        '"limitations":"not enough evidence"}'
    )


def test_tool_use_response_becomes_tool_calls() -> None:
    resp = FakeMessage(
        "tool_use", [FakeToolUseBlock(id="c1", name="query_warehouse", input={"sql": "SELECT 1"})]
    )
    client, _ = _client(resp)
    turn = client.investigate(system="s", tools=(), history=HISTORY)
    assert turn.tool_calls == (
        ToolCallBlock(call_id="c1", tool_name="query_warehouse", arguments={"sql": "SELECT 1"}),
    )
    assert turn.input_tokens == 10
    assert turn.output_tokens == 5
    # FakeUsage's cache fields default to None, matching the real SDK's common
    # case (nothing cache-related to report) - the adapter must not propagate
    # that None into a `ge=0` int field.
    assert turn.cache_read_input_tokens == 0
    assert turn.cache_creation_input_tokens == 0


def test_tool_use_response_with_multiple_calls_extracts_all_of_them() -> None:
    """A parallel tool-use response can mix a leading text block with several
    `tool_use` blocks - the adapter must extract every one of them, in order,
    and ignore the non-`tool_use` block, not just parse the first call. This
    is the real-adapter half of parallel tool use; the agent-loop half (each
    extracted call individually consuming the tool-call budget) is proven in
    `test_investigator.py`."""
    resp = FakeMessage(
        "tool_use",
        [
            FakeTextBlock("I'll check two things."),
            FakeToolUseBlock(id="c1", name="query_warehouse", input={"sql": "SELECT 1"}),
            FakeToolUseBlock(id="c2", name="query_warehouse", input={"sql": "SELECT 2"}),
        ],
    )
    client, _ = _client(resp)
    turn = client.investigate(system="s", tools=(), history=HISTORY)
    assert turn.tool_calls == (
        ToolCallBlock(call_id="c1", tool_name="query_warehouse", arguments={"sql": "SELECT 1"}),
        ToolCallBlock(call_id="c2", tool_name="query_warehouse", arguments={"sql": "SELECT 2"}),
    )


def test_end_turn_response_becomes_a_validated_analysis() -> None:
    resp = FakeMessage("end_turn", [FakeTextBlock(_analysis_json())])
    client, _ = _client(resp)
    turn = client.investigate(system="s", tools=(), history=HISTORY)
    assert turn.is_final
    assert turn.analysis is not None
    assert turn.analysis.outcome is AnalysisOutcome.INCONCLUSIVE


def test_cache_usage_is_threaded_through_when_reported() -> None:
    """The other half of the None-default test: when the API *does* report
    cache activity, the real numbers reach `ModelTurn`, not just a zero."""
    resp = FakeMessage(
        "end_turn",
        [FakeTextBlock(_analysis_json())],
        usage=FakeUsage(cache_read_input_tokens=1_200, cache_creation_input_tokens=340),
    )
    client, _ = _client(resp)
    turn = client.investigate(system="s", tools=(), history=HISTORY)
    assert turn.cache_read_input_tokens == 1_200
    assert turn.cache_creation_input_tokens == 340


def test_tool_use_with_no_tool_use_block_is_a_contract_error() -> None:
    resp = FakeMessage("tool_use", [FakeTextBlock("oops, no tool block")])
    client, _ = _client(resp)
    with pytest.raises(ModelContractError, match="no tool_use block"):
        client.investigate(system="s", tools=(), history=HISTORY)


def test_end_turn_with_no_text_is_a_contract_error() -> None:
    resp = FakeMessage("end_turn", [])
    client, _ = _client(resp)
    with pytest.raises(ModelContractError, match="no text"):
        client.investigate(system="s", tools=(), history=HISTORY)


def test_end_turn_with_unparseable_text_is_a_contract_error() -> None:
    resp = FakeMessage("end_turn", [FakeTextBlock("this is not valid Analysis JSON")])
    client, _ = _client(resp)
    with pytest.raises(ModelContractError, match="did not satisfy the Analysis schema"):
        client.investigate(system="s", tools=(), history=HISTORY)


@pytest.mark.parametrize("stop_reason", ["max_tokens", "model_context_window_exceeded"])
def test_incomplete_stop_reasons_are_contract_errors(stop_reason: str) -> None:
    resp = FakeMessage(stop_reason, [FakeTextBlock("partial")])
    client, _ = _client(resp)
    with pytest.raises(ModelContractError, match="incomplete"):
        client.investigate(system="s", tools=(), history=HISTORY)


def test_unknown_stop_reason_is_a_contract_error() -> None:
    resp = FakeMessage("pause_turn", [])
    client, _ = _client(resp)
    with pytest.raises(ModelContractError, match="unsupported stop_reason"):
        client.investigate(system="s", tools=(), history=HISTORY)


def test_refusal_becomes_model_refusal_error() -> None:
    resp = FakeMessage(
        "refusal", [], stop_details=FakeStopDetails(category="cyber", explanation="no")
    )
    client, _ = _client(resp)
    with pytest.raises(ModelRefusalError) as caught:
        client.investigate(system="s", tools=(), history=HISTORY)
    assert caught.value.context["category"] == "cyber"


# --------------------------------------------------------------------------- #
# Exception mapping - most-specific-first (Engineering Contract 8)
# --------------------------------------------------------------------------- #
def _request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def test_rate_limit_is_transient() -> None:
    resp = httpx2.Response(429, headers={"retry-after": "3"}, request=_request())
    client, _ = _client(exc=anthropic.RateLimitError("rate limited", response=resp, body=None))
    with pytest.raises(ModelTransientError) as caught:
        client.investigate(system="s", tools=(), history=HISTORY)
    assert caught.value.recoverable is True
    assert caught.value.context["retry_after"] == "3"


def test_connection_error_is_transient() -> None:
    client, _ = _client(
        exc=anthropic.APIConnectionError(message="connection reset", request=_request())
    )
    with pytest.raises(ModelTransientError):
        client.investigate(system="s", tools=(), history=HISTORY)


def test_server_error_is_transient() -> None:
    resp = httpx2.Response(500, request=_request())
    client, _ = _client(exc=anthropic.InternalServerError("server error", response=resp, body=None))
    with pytest.raises(ModelTransientError) as caught:
        client.investigate(system="s", tools=(), history=HISTORY)
    assert caught.value.context["status_code"] == 500


def test_client_error_is_not_transient() -> None:
    """A 400 is a caller-side/configuration problem, not something a bounded
    run should treat as retryable."""
    resp = httpx2.Response(400, request=_request())
    client, _ = _client(exc=anthropic.BadRequestError("bad request", response=resp, body=None))
    with pytest.raises(ModelError) as caught:
        client.investigate(system="s", tools=(), history=HISTORY)
    assert not isinstance(caught.value, ModelTransientError)
    assert caught.value.recoverable is False
