"""The ModelClient port's vocabulary: `ModelTurn`, `ConversationMessage`, and
the discriminated content-block union.

Nothing here needs a model, real or fake - these are plain data types, and the
point of this file is that their invariants hold independent of any client
implementation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from causiq.domain import Analysis, AnalysisOutcome, Confidence
from causiq.llm import DEFAULT_SYSTEM_PROMPT_PATH, load_default_system_prompt
from causiq.llm.ports import (
    ConversationMessage,
    MessageRole,
    ModelClient,
    ModelTurn,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)

pytestmark = pytest.mark.unit


def _inconclusive_analysis() -> Analysis:
    return Analysis(
        outcome=AnalysisOutcome.INCONCLUSIVE,
        summary="s",
        limitations="not enough evidence",
        confidence=Confidence.LOW,
    )


# --------------------------------------------------------------------------- #
# ModelTurn - exactly one of two kinds
# --------------------------------------------------------------------------- #
def test_tool_calls_turn_is_not_final() -> None:
    call = ToolCallBlock(call_id="c1", tool_name="query_warehouse", arguments={"reason": "r"})
    turn = ModelTurn(tool_calls=(call,))
    assert turn.is_final is False
    assert turn.analysis is None


def test_analysis_turn_is_final() -> None:
    turn = ModelTurn(analysis=_inconclusive_analysis())
    assert turn.is_final is True
    assert turn.tool_calls == ()


def test_neither_tool_calls_nor_analysis_is_rejected() -> None:
    with pytest.raises(ValidationError, match="never both or neither"):
        ModelTurn()


def test_both_tool_calls_and_analysis_is_rejected() -> None:
    call = ToolCallBlock(call_id="c1", tool_name="t", arguments={})
    with pytest.raises(ValidationError, match="never both or neither"):
        ModelTurn(tool_calls=(call,), analysis=_inconclusive_analysis())


def test_usage_defaults_to_zero_and_must_be_non_negative() -> None:
    turn = ModelTurn(analysis=_inconclusive_analysis())
    assert turn.input_tokens == 0
    assert turn.output_tokens == 0
    assert turn.cache_read_input_tokens == 0
    assert turn.cache_creation_input_tokens == 0
    with pytest.raises(ValidationError):
        ModelTurn(analysis=_inconclusive_analysis(), input_tokens=-1)
    with pytest.raises(ValidationError):
        ModelTurn(analysis=_inconclusive_analysis(), cache_read_input_tokens=-1)


def test_cache_usage_fields_are_additive_and_optional() -> None:
    """A `FakeModelClient` script written before P0.7 - a bare `ModelTurn`
    with no cache fields - must still construct cleanly, and a script that
    does report cache usage must carry it through untouched."""
    turn = ModelTurn(
        analysis=_inconclusive_analysis(),
        cache_read_input_tokens=500,
        cache_creation_input_tokens=25,
    )
    assert turn.cache_read_input_tokens == 500
    assert turn.cache_creation_input_tokens == 25


def test_model_turn_is_frozen() -> None:
    turn = ModelTurn(analysis=_inconclusive_analysis())
    with pytest.raises(ValidationError):
        turn.input_tokens = 5


# --------------------------------------------------------------------------- #
# Content blocks and the discriminated union
# --------------------------------------------------------------------------- #
def test_conversation_message_requires_at_least_one_block() -> None:
    with pytest.raises(ValidationError):
        ConversationMessage(role=MessageRole.USER, content=())


def test_content_blocks_round_trip_through_json() -> None:
    """The union must be reconstructible from its own serialized form - the
    discriminator, not best-effort field matching, is what makes this safe."""
    message = ConversationMessage(
        role=MessageRole.ASSISTANT,
        content=(
            TextBlock(text="thinking out loud"),
            ToolCallBlock(call_id="c1", tool_name="query_warehouse", arguments={"reason": "r"}),
        ),
    )
    restored = ConversationMessage.model_validate_json(message.model_dump_json())
    assert restored == message
    assert isinstance(restored.content[0], TextBlock)
    assert isinstance(restored.content[1], ToolCallBlock)


def test_tool_result_block_round_trips() -> None:
    message = ConversationMessage(
        role=MessageRole.USER,
        content=(ToolResultBlock(call_id="c1", content="evidence_id: ev_001", is_error=False),),
    )
    restored = ConversationMessage.model_validate_json(message.model_dump_json())
    assert isinstance(restored.content[0], ToolResultBlock)
    assert restored.content[0].call_id == "c1"


def test_tool_call_arguments_are_untrusted_arbitrary_json() -> None:
    """Arguments are whatever the model produced - the block does not
    validate their shape against any tool's schema (that is the executor's
    job, downstream, per invariant I3)."""
    call = ToolCallBlock(
        call_id="c1", tool_name="query_warehouse", arguments={"sql": "DROP TABLE x", "n": 1}
    )
    assert call.arguments == {"sql": "DROP TABLE x", "n": 1}


def test_content_blocks_are_frozen() -> None:
    block = TextBlock(text="hi")
    with pytest.raises(ValidationError):
        block.text = "changed"


def test_text_block_rejects_empty_text() -> None:
    with pytest.raises(ValidationError):
        TextBlock(text="")


def test_model_client_is_a_runtime_checkable_protocol() -> None:
    """A conforming object need not inherit from ModelClient - matching
    every other port in this codebase (Clock, Tool, AuditSink)."""

    class Conforms:
        def investigate(
            self,
            *,
            system: str,
            tools: tuple[dict[str, object], ...],
            history: tuple[ConversationMessage, ...],
        ) -> ModelTurn:
            return ModelTurn(analysis=_inconclusive_analysis())

    assert isinstance(Conforms(), ModelClient)

    class DoesNotConform:
        pass

    assert not isinstance(DoesNotConform(), ModelClient)


# --------------------------------------------------------------------------- #
# The frozen default system prompt (Engineering Contract 6.3)
# --------------------------------------------------------------------------- #
def test_default_system_prompt_loads_and_is_non_trivial() -> None:
    text = load_default_system_prompt()
    assert isinstance(text, str)
    assert len(text) > 100
    assert "evidence" in text.lower()


def test_default_system_prompt_path_points_at_a_real_file() -> None:
    assert DEFAULT_SYSTEM_PROMPT_PATH.is_file()
    assert DEFAULT_SYSTEM_PROMPT_PATH.name == "investigator_system.md"
