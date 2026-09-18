"""`causiq.runner` - the thin operator layer behind the CLI (P0.7).

These tests exercise the *real* stack throughout: the real `Investigator`,
the real `ToolRegistry`/`ToolExecutor`/AuthZ path, and a real (temp) DuckDB
warehouse. Only the model is scripted, via `FakeModelClient`, so every test
here is offline and deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from causiq.clock import FrozenClock
from causiq.config import Settings
from causiq.domain import InvestigationRun, RunState
from causiq.errors import ConfigurationError
from causiq.ids import FixedIdGenerator
from causiq.llm.anthropic_client import AnthropicModelClient
from causiq.llm.fake_client import FakeModelClient
from causiq.runner import build_model_client, build_registry, run_investigation

pytestmark = pytest.mark.unit


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        audit_dir=tmp_path / "audit",
        runs_dir=tmp_path / "runs",
        **overrides,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Model client selection
# --------------------------------------------------------------------------- #
def test_dry_run_selects_the_fake_model_client(tmp_path: Path) -> None:
    client = build_model_client(dry_run=True, settings=_settings(tmp_path))
    assert isinstance(client, FakeModelClient)


def test_live_selects_the_anthropic_model_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key-000000000000")
    settings = _settings(tmp_path)
    client = build_model_client(dry_run=False, settings=settings)
    assert isinstance(client, AnthropicModelClient)


def test_live_without_a_key_raises_configuration_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path, anthropic_api_key=None)
    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY is not set"):
        build_model_client(dry_run=False, settings=settings)


# --------------------------------------------------------------------------- #
# Registry / warehouse
# --------------------------------------------------------------------------- #
def test_build_registry_creates_the_warehouse_file_if_missing(tmp_path: Path) -> None:
    warehouse_path = tmp_path / "warehouse" / "inc001.duckdb"
    assert not warehouse_path.exists()
    registry = build_registry(warehouse_path=warehouse_path)
    assert warehouse_path.exists()
    assert "query_warehouse" in registry.names()


def test_build_registry_does_not_rebuild_an_existing_warehouse(
    tmp_path: Path, warehouse_db_path: Path
) -> None:
    """Idempotent: a second call against an already-built file must not try
    to recreate it (`build_warehouse_db` refuses to overwrite)."""
    before = warehouse_db_path.stat().st_mtime
    build_registry(warehouse_path=warehouse_db_path)
    assert warehouse_db_path.stat().st_mtime == before


# --------------------------------------------------------------------------- #
# run_investigation - the real stack, offline
# --------------------------------------------------------------------------- #
def test_dry_run_investigation_completes_and_persists_both_artifacts(
    tmp_path: Path, warehouse_db_path: Path
) -> None:
    settings = _settings(tmp_path)
    artifacts = run_investigation(
        "INC-001",
        dry_run=True,
        settings=settings,
        id_generator=FixedIdGenerator(),
        clock=FrozenClock(),
        warehouse_path=warehouse_db_path,
    )

    run = artifacts.run
    assert run.state is RunState.COMPLETED
    assert len(run.evidence) == 3
    assert run.analysis is not None

    assert artifacts.audit_path.exists()
    assert artifacts.audit_path.read_text(encoding="utf-8").strip() != ""
    assert "run.started" in artifacts.audit_path.read_text(encoding="utf-8")

    assert artifacts.result_path.exists()
    persisted = InvestigationRun.model_validate_json(
        artifacts.result_path.read_text(encoding="utf-8")
    )
    # Read back from disk, not just held in memory - this is the actual
    # AC-9 property: the evidence survives the process, not merely the call.
    assert persisted == run
    assert len(persisted.evidence) == 3
    for evidence in persisted.evidence:
        assert evidence.verify()


def test_unknown_incident_raises_before_touching_the_warehouse(tmp_path: Path) -> None:
    warehouse_path = tmp_path / "warehouse" / "inc001.duckdb"
    settings = _settings(tmp_path)
    with pytest.raises(FileNotFoundError):
        run_investigation(
            "NOT-A-REAL-INCIDENT",
            dry_run=True,
            settings=settings,
            warehouse_path=warehouse_path,
        )
    assert not warehouse_path.exists(), "the warehouse must not be built for an unknown incident"
    # No audit/result artifacts for an investigation that never started.
    assert not settings.audit_dir.exists() or not any(settings.audit_dir.iterdir())
    assert not settings.runs_dir.exists() or not any(settings.runs_dir.iterdir())


def test_live_without_key_raises_before_touching_the_warehouse(tmp_path: Path) -> None:
    warehouse_path = tmp_path / "warehouse" / "inc001.duckdb"
    settings = _settings(tmp_path, anthropic_api_key=None)
    with pytest.raises(ConfigurationError):
        run_investigation(
            "INC-001",
            dry_run=False,
            settings=settings,
            warehouse_path=warehouse_path,
        )
    assert not warehouse_path.exists(), "the warehouse must not be built when the key check fails"


def test_budget_exceeded_run_still_persists_its_partial_evidence(
    tmp_path: Path, warehouse_db_path: Path
) -> None:
    """AC-9's critical case: a run that never reaches a final analysis must
    still leave a result record on disk, containing whatever real evidence
    it collected before the budget stopped it."""
    settings = _settings(tmp_path, budget_max_turns=1)
    artifacts = run_investigation(
        "INC-001",
        dry_run=True,
        settings=settings,
        id_generator=FixedIdGenerator(),
        clock=FrozenClock(),
        warehouse_path=warehouse_db_path,
    )

    run = artifacts.run
    assert run.state is RunState.BUDGET_EXCEEDED
    assert run.analysis is None
    assert len(run.evidence) == 1  # the one tool call the single allowed turn made

    persisted = InvestigationRun.model_validate_json(
        artifacts.result_path.read_text(encoding="utf-8")
    )
    assert persisted.state is RunState.BUDGET_EXCEEDED
    assert len(persisted.evidence) == 1
    assert persisted.evidence[0].verify()
