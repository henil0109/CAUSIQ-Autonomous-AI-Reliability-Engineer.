"""Opt-in live tests against the real Anthropic API.

Skipped automatically unless `ANTHROPIC_API_KEY` is set - see the
`pytest_collection_modifyitems` hook in `tests/conftest.py`, which applies to
every `live`-marked test in the suite, not something reimplemented here.

Scope is deliberately narrow (Engineering Contract, P0.6 11): these verify the
adapter's own contract - that a real request/response round trip translates
correctly - not a full investigation. `test_investigator.py` and
`test_inc001_agent_investigation.py` already prove the agent loop itself,
offline and deterministically, with `FakeModelClient`; nothing about the loop
needs to be re-proven against the live API.

The API key is never read, printed, or logged by this file - it flows
directly from `causiq.config.Settings` (a `SecretStr`) into the SDK via
`AnthropicModelClient.from_settings`, and this file never touches it.
"""

from __future__ import annotations

import pytest
from pydantic import JsonValue

from causiq.config import load_settings
from causiq.domain import AnalysisOutcome
from causiq.llm.anthropic_client import AnthropicModelClient
from causiq.llm.ports import ConversationMessage, MessageRole, TextBlock, ToolResultBlock

pytestmark = pytest.mark.live


@pytest.fixture
def client() -> AnthropicModelClient:
    return AnthropicModelClient.from_settings(load_settings())


def test_live_produces_a_structured_final_analysis(client: AnthropicModelClient) -> None:
    """No tools are offered, so the only well-formed response is a final,
    schema-valid `Analysis` - this is the adapter's structured-output
    contract, exercised against the real API."""
    system = (
        "You are a test harness. No tools are available. Immediately conclude with an "
        "INCONCLUSIVE analysis stating there was insufficient evidence, since this is a "
        "connectivity test, not a real investigation."
    )
    history = (
        ConversationMessage(
            role=MessageRole.USER,
            content=(TextBlock(text="This is an adapter contract test. Conclude immediately."),),
        ),
    )

    turn = client.investigate(system=system, tools=(), history=history)

    assert turn.is_final
    assert turn.analysis is not None
    assert turn.analysis.outcome in (AnalysisOutcome.INCONCLUSIVE, AnalysisOutcome.COMPLETED)
    assert turn.input_tokens > 0
    assert turn.output_tokens > 0


def test_live_can_request_a_registered_tool(client: AnthropicModelClient) -> None:
    """One trivial tool is offered and the system prompt requires using it -
    this is the adapter's tool-use contract, exercised against the real API.
    """
    system = (
        "You are a test harness verifying tool use. You MUST call the "
        "`ping` tool exactly once before doing anything else. Do not explain, just call it."
    )
    tools: tuple[dict[str, JsonValue], ...] = (
        {
            "name": "ping",
            "description": "A no-op connectivity check. Always call this first.",
            "input_schema": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    )
    history = (
        ConversationMessage(
            role=MessageRole.USER, content=(TextBlock(text="Begin the connectivity check."),)
        ),
    )

    turn = client.investigate(system=system, tools=tools, history=history)

    assert not turn.is_final
    assert len(turn.tool_calls) >= 1
    assert turn.tool_calls[0].tool_name == "ping"
    assert turn.tool_calls[0].call_id


def test_live_second_call_hits_the_prompt_cache(client: AnthropicModelClient) -> None:
    """Engineering Contract 6.3's cache-stability guarantee, proven against
    the real API (P0.7): a second call sharing an identical system+tools
    prefix must report `cache_read_input_tokens > 0`.

    Two shape decisions worth recording, since the actual API contract
    dictated them rather than convenience:

    * The system prompt below is deliberately padded well past Anthropic's
      documented minimum cacheable prefix length (on the order of 1024
      tokens for Opus-tier models). A short, natural-sounding prompt like
      "you are a test harness" is valid text but too small to ever be
      cached, which would make this test assert nothing about caching at
      all - it would just prove `cache_read_input_tokens` defaults to 0.
    * The first call requests a tool rather than concluding immediately, so
      the second call's `history` grows the way a real investigation turn
      actually grows (a tool_use block, then its tool_result) - the same
      shape `Investigator._run_loop` produces, not an artificial one built
      only for this test.

    `tools` and `system` are passed byte-identical on both calls, and the
    growing history is appended strictly after the cache breakpoint
    (Contract 6.3 item 3), so nothing this test does should itself be able
    to suppress a cache hit.
    """
    system = (
        "You are a test harness verifying Causiq's prompt-cache stability "
        "(Engineering Contract 6.3). You MUST call the `ping` tool exactly "
        "once before doing anything else. Do not explain, just call it. "
    ) + (
        "This sentence pads the system prompt well past Anthropic's minimum "
        "cacheable prefix length so the cache is actually exercised by this "
        "test rather than trivially reporting zero. " * 40
    )
    tools: tuple[dict[str, JsonValue], ...] = (
        {
            "name": "ping",
            "description": "A no-op connectivity check. Always call this first.",
            "input_schema": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    )
    first_history = (
        ConversationMessage(role=MessageRole.USER, content=(TextBlock(text="Begin."),)),
    )
    first_turn = client.investigate(system=system, tools=tools, history=first_history)
    assert not first_turn.is_final
    assert first_turn.tool_calls

    call = first_turn.tool_calls[0]
    second_history = (
        *first_history,
        ConversationMessage(role=MessageRole.ASSISTANT, content=(call,)),
        ConversationMessage(
            role=MessageRole.USER,
            content=(ToolResultBlock(call_id=call.call_id, content="pong", is_error=False),),
        ),
    )
    second_turn = client.investigate(system=system, tools=tools, history=second_history)

    assert second_turn.cache_read_input_tokens > 0, (
        "the second call reported no cache hit - either the system+tools prefix "
        "was not byte-identical across calls, or it was still under Anthropic's "
        "minimum cacheable length despite the padding above"
    )
