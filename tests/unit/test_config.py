"""Configuration loading, validation, and secret handling."""

from __future__ import annotations

import pytest

from causiq.config import Settings, load_settings
from causiq.errors import ConfigurationError

pytestmark = pytest.mark.unit

FAKE_KEY = "sk-ant-notarealkey-000000000000"


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """Never read the developer's real `.env` or environment during tests."""
    for name in list(Settings.model_fields):
        monkeypatch.delenv(f"CAUSIQ_{name.upper()}", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]


def test_defaults_match_the_documented_model_policy() -> None:
    """Engineering Contract 6.1/6.2 - defaults are part of the contract."""
    settings = load_settings()
    assert settings.model_id == "claude-opus-5"
    assert settings.model_effort == "high"
    assert settings.model_max_tokens == 16_000
    assert settings.environment == "local"
    assert settings.log_format == "json"


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAUSIQ_ENVIRONMENT", "ci")
    monkeypatch.setenv("CAUSIQ_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("CAUSIQ_BUDGET_MAX_TURNS", "4")
    settings = load_settings()
    assert settings.environment == "ci"
    assert settings.log_level == "DEBUG"
    assert settings.budget_max_turns == 4


def test_invalid_value_fails_fast_with_a_readable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup validation, not a surprise at turn one of an investigation."""
    monkeypatch.setenv("CAUSIQ_BUDGET_MAX_TURNS", "not-a-number")
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert "budget_max_turns" in str(caught.value)
    assert caught.value.context["problem_count"] == 1


def test_multiple_problems_are_reported_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAUSIQ_BUDGET_MAX_TURNS", "0")
    monkeypatch.setenv("CAUSIQ_LOG_LEVEL", "LOUD")
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert caught.value.context["problem_count"] == 2


def test_configuration_error_is_fatal() -> None:
    assert ConfigurationError.recoverable is False


def test_budget_limits_are_validated_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAUSIQ_BUDGET_DEADLINE_SECONDS", "-1")
    with pytest.raises(ConfigurationError, match="budget_deadline_seconds"):
        load_settings()


def test_default_budget_is_built_from_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAUSIQ_BUDGET_MAX_TURNS", "7")
    monkeypatch.setenv("CAUSIQ_BUDGET_MAX_TOOL_CALLS", "9")
    budget = load_settings().default_budget()
    assert budget.max_turns == 7
    assert budget.max_tool_calls == 9


def test_has_api_key_is_false_when_unset() -> None:
    assert load_settings().has_api_key is False


def test_has_api_key_is_false_for_a_blank_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key set to whitespace is not a key. This is what the live-test gate reads."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    assert load_settings().has_api_key is False


def test_api_key_is_read_from_the_sdk_variable_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    assert load_settings().has_api_key is True


def test_api_key_never_appears_in_repr_or_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key must not leak through logs, error output, or audit records.

    `SecretStr` is the mechanism; this test is the proof, and it will fail loudly
    if someone changes the field to a plain `str`.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    settings = load_settings()
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings)
    assert FAKE_KEY not in settings.model_dump_json()
    assert FAKE_KEY not in str(settings.model_dump())
    # Still retrievable deliberately, for the one caller that needs it.
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == FAKE_KEY


def test_settings_are_immutable() -> None:
    settings = load_settings()
    with pytest.raises(ValueError, match=r"frozen|immutable"):
        settings.model_id = "claude-sonnet-5"


def test_overrides_are_accepted_for_tests_and_cli() -> None:
    settings = load_settings(environment="prod", log_format="console")
    assert settings.environment == "prod"
    assert settings.log_format == "console"


def test_unknown_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """`extra="ignore"` - an unrelated CAUSIQ_* variable must not crash startup."""
    monkeypatch.setenv("CAUSIQ_SOMETHING_FUTURE", "1")
    assert load_settings().environment == "local"
