"""Environment-driven configuration, validated once at process start.

Maturity: hardened.

Why fail-fast validation matters here more than usual: an investigation is a
long-running, expensive, partially non-deterministic process. Discovering that
`CAUSIQ_BUDGET_MAX_TURNS` is `"twelve"` at turn one wastes a model call; being
told at startup costs nothing. `load_settings()` therefore converts pydantic's
`ValidationError` into a `ConfigurationError` with a readable, aggregated message.

Secret handling: `anthropic_api_key` is a `SecretStr`, so it does not appear in
`repr()`, `str()`, or `model_dump_json()`. Causiq never passes it to the SDK
explicitly - the SDK reads `ANTHROPIC_API_KEY` from the environment itself. We
hold it only to answer "is a key configured?" without reading the environment
twice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from causiq.domain.budget import Budget
from causiq.errors import ConfigurationError

Environment = Literal["local", "ci", "prod"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogFormat = Literal["json", "console"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    """The complete configuration surface. Immutable once loaded."""

    model_config = SettingsConfigDict(
        env_prefix="CAUSIQ_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        # Fields named `model_*` are ours, not pydantic's.
        protected_namespaces=(),
    )

    # --- Anthropic ---------------------------------------------------------
    # Unprefixed: the SDK's own variable name, so one value serves both.
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="ANTHROPIC_API_KEY",
    )

    # --- Runtime -----------------------------------------------------------
    environment: Environment = "local"
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "json"

    # --- Model policy (Engineering Contract 6.1 / 6.2) ---------------------
    model_id: str = "claude-opus-5"
    model_effort: Effort = "high"
    model_max_tokens: int = Field(default=16_000, gt=0, le=128_000)

    # --- Run budget (invariant I5) -----------------------------------------
    budget_max_turns: int = Field(default=12, gt=0)
    budget_max_tool_calls: int = Field(default=24, gt=0)
    budget_max_total_tokens: int = Field(default=400_000, gt=0)
    budget_deadline_seconds: float = Field(default=600.0, gt=0)

    # --- Storage -----------------------------------------------------------
    audit_dir: Path = Path("var/audit")
    #: Where the CLI runner (P0.7) persists each run's terminal
    #: `InvestigationRun` record - including its evidence, which is what
    #: makes AC-9's "persisted partial ledger" true rather than aspirational.
    #: See `causiq.runner` module docstring for the persistence boundary.
    runs_dir: Path = Path("var/runs")

    @property
    def has_api_key(self) -> bool:
        """Whether a key is configured, without exposing its value.

        The live test suite skips on this; the offline suite never consults it.
        """
        secret = self.anthropic_api_key
        return secret is not None and bool(secret.get_secret_value().strip())

    def default_budget(self) -> Budget:
        """The run budget implied by configuration."""
        return Budget(
            max_turns=self.budget_max_turns,
            max_tool_calls=self.budget_max_tool_calls,
            max_total_tokens=self.budget_max_total_tokens,
            deadline_seconds=self.budget_deadline_seconds,
        )


def load_settings(**overrides: object) -> Settings:
    """Load and validate settings, or fail with a readable `ConfigurationError`.

    `overrides` exists for tests and for the CLI's explicit flags; it is not a
    back door for production configuration.
    """
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors()
        )
        msg = f"invalid Causiq configuration: {problems}"
        raise ConfigurationError(msg, problem_count=exc.error_count()) from exc
