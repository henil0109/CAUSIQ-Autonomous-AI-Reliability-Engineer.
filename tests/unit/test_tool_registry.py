"""Tool registry behaviour.

The registry is a closed set: it is what makes "the model cannot call an arbitrary
Python function" true (invariant I3). It is also the front of the cached prompt
prefix, so its output has to be byte-stable.
"""

from __future__ import annotations

import pytest

from causiq.errors import ToolRegistrationError, UnknownToolError
from causiq.tools import ToolRegistry, canonical_json, tool_schema
from tests.doubles import EchoTool, ExplodingTool, ForeignInputTool, LooseTool, MutatingTool

pytestmark = pytest.mark.unit


@pytest.fixture
def registry() -> ToolRegistry:
    book = ToolRegistry()
    book.register(EchoTool())
    book.register(MutatingTool())
    return book


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def test_registers_and_resolves_tools(registry: ToolRegistry) -> None:
    assert len(registry) == 2
    assert "echo_reader" in registry
    assert registry.get("echo_reader") is not None
    assert registry.require("echo_reader").spec.name == "echo_reader"


def test_duplicate_name_is_rejected() -> None:
    """A shadowed tool would mean authorizing one tool and executing another."""
    book = ToolRegistry()
    book.register(EchoTool())
    with pytest.raises(ToolRegistrationError, match="already registered") as caught:
        book.register(EchoTool())
    assert caught.value.context["name"] == "echo_reader"
    assert len(book) == 1


def test_duplicate_is_rejected_even_for_a_different_implementation() -> None:
    """Identity is the name, not the class - that is what the model addresses."""
    book = ToolRegistry()
    book.register(EchoTool(name="shared_name"))
    with pytest.raises(ToolRegistrationError, match="already registered"):
        book.register(ExplodingTool(name="shared_name"))


def test_registration_failure_is_fatal_not_recoverable() -> None:
    """The tool surface is fixed before a run starts, so this can never be
    something a run recovers from mid-flight."""
    assert ToolRegistrationError.recoverable is False


@pytest.mark.parametrize(
    "bad_name",
    ["Echo", "echo-reader", "ec", "1echo", "echo reader", "", "echo__reader!"],
)
def test_malformed_names_are_rejected(bad_name: str) -> None:
    book = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="snake_case"):
        book.register(EchoTool(name=bad_name))


def test_input_model_must_subclass_tool_input() -> None:
    """Without the base class there is no guaranteed `reason` field, and evidence
    would lose the provenance that makes it worth recording."""
    book = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="must subclass") as caught:
        book.register(ForeignInputTool())
    assert caught.value.context["name"] == "foreign_reader"


def test_input_model_must_be_strict_eligible() -> None:
    """Without extra="forbid" pydantic omits `additionalProperties: false`, and
    the API rejects the strict tool definition."""
    book = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="strict-eligible"):
        book.register(LooseTool())


def test_register_all_stops_at_the_first_problem() -> None:
    book = ToolRegistry()
    with pytest.raises(ToolRegistrationError):
        book.register_all((EchoTool(), EchoTool(), MutatingTool()))
    assert registry_names(book) == ("echo_reader",)


def registry_names(book: ToolRegistry) -> tuple[str, ...]:
    return book.names()


# --------------------------------------------------------------------------- #
# Unknown tools
# --------------------------------------------------------------------------- #
def test_get_returns_none_for_unknown(registry: ToolRegistry) -> None:
    assert registry.get("nonexistent") is None
    assert "nonexistent" not in registry


def test_require_raises_and_lists_what_is_available(registry: ToolRegistry) -> None:
    """Telling the model what it *could* have called is what lets it recover."""
    with pytest.raises(UnknownToolError) as caught:
        registry.require("nonexistent")
    assert caught.value.context["name"] == "nonexistent"
    assert caught.value.context["available"] == ["echo_reader", "mutating_writer"]
    assert caught.value.recoverable is True


# --------------------------------------------------------------------------- #
# Determinism - prompt-cache stability (Engineering Contract 6.3)
# --------------------------------------------------------------------------- #
def test_names_are_sorted_not_insertion_ordered() -> None:
    book = ToolRegistry()
    book.register(MutatingTool(name="zulu_writer"))
    book.register(EchoTool(name="alpha_reader"))
    assert book.names() == ("alpha_reader", "zulu_writer")


def test_iteration_follows_name_order() -> None:
    book = ToolRegistry()
    book.register(MutatingTool(name="zulu_writer"))
    book.register(EchoTool(name="alpha_reader"))
    assert [tool.spec.name for tool in book] == ["alpha_reader", "zulu_writer"]


def test_registration_order_does_not_affect_emitted_schemas() -> None:
    """The single most important determinism property in the file.

    Tools render before the system prompt and the messages, so if registration
    order leaked into the output, changing it would silently invalidate the whole
    prompt cache for every turn that followed.
    """
    forward = ToolRegistry()
    forward.register(EchoTool())
    forward.register(MutatingTool())

    reverse = ToolRegistry()
    reverse.register(MutatingTool())
    reverse.register(EchoTool())

    assert forward.schemas() == reverse.schemas()
    assert forward.schema_digest() == reverse.schema_digest()


def test_schema_emission_is_byte_stable_across_calls(registry: ToolRegistry) -> None:
    first = canonical_json(list(registry.schemas()))
    for _ in range(5):
        assert canonical_json(list(registry.schemas())) == first
    assert registry.schema_digest() == registry.schema_digest()


def test_digest_changes_when_the_tool_surface_changes(registry: ToolRegistry) -> None:
    """A changed digest means a changed cached prefix - it must not be silent."""
    before = registry.schema_digest()
    registry.register(EchoTool(name="another_reader"))
    assert registry.schema_digest() != before


def test_empty_registry_is_valid_and_stable() -> None:
    book = ToolRegistry()
    assert book.schemas() == ()
    assert book.names() == ()
    assert book.schema_digest() == ToolRegistry().schema_digest()


# --------------------------------------------------------------------------- #
# Schema shape
# --------------------------------------------------------------------------- #
def test_emitted_schema_is_strict_and_complete() -> None:
    schema = tool_schema(EchoTool())
    assert schema["name"] == "echo_reader"
    assert schema["strict"] is True
    assert schema["description"]

    input_schema = schema["input_schema"]
    assert input_schema["additionalProperties"] is False
    # `reason` is inherited from ToolInput and is required on every tool.
    assert set(input_schema["required"]) == {"reason", "value"}
    assert "times" in input_schema["properties"]  # optional, so not required


def test_every_tool_requires_a_reason(registry: ToolRegistry) -> None:
    """The explainability signal is structural, not a convention."""
    for schema in registry.schemas():
        assert "reason" in schema["input_schema"]["required"], schema["name"]
