"""The model boundary: a provider-agnostic port, and the adapters behind it.

Maturity: hardened for the port and the translation logic; the real Anthropic
adapter is untested against the live API in the default suite (ADR-0001).

Nothing outside `causiq.llm.anthropic_client` imports the `anthropic` package.
The agent (`causiq.agents.investigator`) depends only on `ModelClient` and the
plain types in `causiq.llm.ports` - it could be pointed at any provider that
can implement the same small contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from causiq.llm.fake_client import FakeModelClient
from causiq.llm.ports import (
    ConversationMessage,
    MessageRole,
    ModelClient,
    ModelTurn,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)

#: The frozen, versioned investigator system prompt (Engineering Contract
#: 6.3: no timestamps, no run ids, no counters, so it stays a stable prefix
#: for prompt caching). Lives here rather than under `causiq.agents` because
#: it is part of the model boundary, not the loop that drives it.
DEFAULT_SYSTEM_PROMPT_PATH: Final = (
    Path(__file__).resolve().parent / "prompts" / "investigator_system.md"
)


def load_default_system_prompt() -> str:
    """Read the frozen investigator system prompt from disk."""
    return DEFAULT_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


__all__ = [
    "DEFAULT_SYSTEM_PROMPT_PATH",
    "ConversationMessage",
    "FakeModelClient",
    "MessageRole",
    "ModelClient",
    "ModelTurn",
    "TextBlock",
    "ToolCallBlock",
    "ToolResultBlock",
    "load_default_system_prompt",
]
