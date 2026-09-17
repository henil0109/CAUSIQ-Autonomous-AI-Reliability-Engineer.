"""`FakeModelClient` - the scripted, deterministic ModelClient for offline tests.

These tests are about the fake's *own* mechanics (playback order, exhaustion,
the three script-entry kinds); `test_investigator.py` and
`test_inc001_agent_investigation.py` are where it is used to drive a real
agent.
"""

from __future__ import annotations

import pytest
from pydantic import JsonValue

from causiq.domain import Analysis, AnalysisOutcome, Claim, Confidence
from causiq.errors import ModelContractError
from causiq.ids import EvidenceId
from causiq.llm.fake_client import FakeModelClient
from causiq.llm.ports import ConversationMessage, MessageRole, ModelTurn, TextBlock, ToolCallBlock

pytestmark = pytest.mark.unit


def _analysis() -> Analysis:
    return Analysis(
        outcome=AnalysisOutcome.INCONCLUSIVE,
        summary="s",
        limitations="l",
        confidence=Confidence.LOW,
    )


def _hist(*text: str) -> tuple[ConversationMessage, ...]:
    return tuple(
        ConversationMessage(role=MessageRole.USER, content=(TextBlock(text=t),)) for t in text
    )


# --------------------------------------------------------------------------- #
# Scripted playback, in order
# --------------------------------------------------------------------------- #
def test_plays_back_scripted_turns_in_order() -> None:
    turn1 = ModelTurn(tool_calls=(ToolCallBlock(call_id="c1", tool_name="t", arguments={}),))
    turn2 = ModelTurn(analysis=_analysis())
    fake = FakeModelClient([turn1, turn2])

    first = fake.investigate(system="s", tools=(), history=_hist("a"))
    second = fake.investigate(system="s", tools=(), history=_hist("a", "b"))

    assert first is turn1
    assert second is turn2
    assert fake.calls_made == 2
    assert fake.remaining == 0


def test_captures_what_each_call_was_given() -> None:
    fake = FakeModelClient([ModelTurn(analysis=_analysis())])
    tools: tuple[dict[str, JsonValue], ...] = ({"name": "query_warehouse"},)
    history = _hist("incident brief")

    fake.investigate(system="be honest", tools=tools, history=history)

    assert len(fake.calls) == 1
    captured = fake.calls[0]
    assert captured.system == "be honest"
    assert captured.tools == tools
    assert captured.history == history


# --------------------------------------------------------------------------- #
# Exhaustion is a test-authoring bug, not a modeled failure
# --------------------------------------------------------------------------- #
def test_exhausted_script_raises_a_plain_runtime_error() -> None:
    fake = FakeModelClient([ModelTurn(analysis=_analysis())])
    fake.investigate(system="s", tools=(), history=_hist("a"))

    with pytest.raises(RuntimeError, match="script exhausted"):
        fake.investigate(system="s", tools=(), history=_hist("a", "b"))


def test_exhaustion_is_not_a_causiq_model_error() -> None:
    """Deliberately not `ModelContractError` - an agent's `except ModelError`
    handler must not silently absorb a test-authoring mistake."""
    fake = FakeModelClient([])
    with pytest.raises(RuntimeError):
        fake.investigate(system="s", tools=(), history=_hist("a"))
    with pytest.raises(Exception) as caught:
        FakeModelClient([]).investigate(system="s", tools=(), history=_hist("a"))
    assert not isinstance(caught.value, ModelContractError)


# --------------------------------------------------------------------------- #
# Callable entries: the only way to cite ids minted at run time
# --------------------------------------------------------------------------- #
def test_callable_entry_receives_the_current_history() -> None:
    seen: list[tuple[ConversationMessage, ...]] = []

    def final_turn(history: tuple[ConversationMessage, ...]) -> ModelTurn:
        seen.append(history)
        return ModelTurn(analysis=_analysis())

    fake = FakeModelClient([final_turn])
    history = _hist("evidence_id: ev_001\n...")
    fake.investigate(system="s", tools=(), history=history)

    assert seen == [history]


def test_callable_entry_can_build_a_response_from_evidence_ids_in_history() -> None:
    """The exact pattern INC-001 tests rely on: the final analysis cites an id
    that only exists because an earlier scripted tool call produced it."""

    def final_turn(history: tuple[ConversationMessage, ...]) -> ModelTurn:
        # Find the evidence id the way the real model would: by reading it out
        # of a tool result already in the conversation.
        for message in history:
            for block in message.content:
                text: str = getattr(block, "content", "") or getattr(block, "text", "")
                if "evidence_id: ev_001" in text:
                    return ModelTurn(
                        analysis=Analysis(
                            outcome=AnalysisOutcome.COMPLETED,
                            summary="s",
                            root_cause="rc",
                            confidence=Confidence.HIGH,
                            claims=(Claim(statement="x", citations=(EvidenceId("ev_001"),)),),
                        )
                    )
        raise AssertionError("evidence id not found in history")

    fake = FakeModelClient([final_turn])
    history = (
        ConversationMessage(
            role=MessageRole.USER,
            content=(TextBlock(text="evidence_id: ev_001\ncontent_type: text/plain\n\n42"),),
        ),
    )
    turn = fake.investigate(system="s", tools=(), history=history)
    assert turn.analysis is not None
    assert turn.analysis.claims[0].citations == ("ev_001",)


# --------------------------------------------------------------------------- #
# Malformed responses, scripted as exceptions
# --------------------------------------------------------------------------- #
def test_exception_entry_is_raised_not_returned() -> None:
    fake = FakeModelClient([ModelContractError("garbage response")])
    with pytest.raises(ModelContractError, match="garbage response"):
        fake.investigate(system="s", tools=(), history=_hist("a"))


def test_mixed_script_of_turns_and_exceptions() -> None:
    call = ToolCallBlock(call_id="c1", tool_name="t", arguments={})
    fake = FakeModelClient(
        [ModelTurn(tool_calls=(call,)), ModelContractError("oops"), ModelTurn(analysis=_analysis())]
    )
    first = fake.investigate(system="s", tools=(), history=_hist("a"))
    assert first.tool_calls == (call,)

    with pytest.raises(ModelContractError, match="oops"):
        fake.investigate(system="s", tools=(), history=_hist("a", "b"))

    # the script continues past the exception entry
    third = fake.investigate(system="s", tools=(), history=_hist("a", "b", "c"))
    assert third.is_final
