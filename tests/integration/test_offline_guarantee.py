"""The offline guarantee - invariant I6, AC-12, AC-14.

The default test suite must run with no API key and no network. These tests
assert that property directly rather than relying on it happening to be true.
"""

from __future__ import annotations

import importlib
import pkgutil
import socket
from pathlib import Path
from typing import Any

import pytest

import causiq
from causiq.cli import main as cli_main

pytestmark = pytest.mark.integration


def test_every_module_imports_without_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing Causiq must never require credentials.

    A module that constructs an SDK client at import time would fail here, which
    is exactly the regression this guards against as the Anthropic adapter lands
    in P0.6.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for module in pkgutil.walk_packages(causiq.__path__, prefix="causiq."):
        importlib.import_module(module.name)


def test_configuration_loads_with_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from causiq.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.has_api_key is False
    assert settings.model_id == "claude-opus-5"
    assert settings.default_budget().max_turns > 0


def test_no_network_access_is_attempted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sever the socket layer, then exercise the P0 code paths.

    If any of them reached the network, this would raise instead of passing.
    """

    def forbidden(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        msg = "the offline test suite must not open a network connection"
        raise AssertionError(msg)

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    from datetime import UTC, datetime

    from causiq.authz import AgentIdentity, CapabilityRequest, Permission, authorize
    from causiq.domain import AgentRole, EvidenceRequest, EvidenceSource
    from causiq.evidence import EvidenceLedger, validate_citations
    from causiq.ids import RunId
    from tests.conftest import make_analysis

    identity = AgentIdentity(
        agent_id="agent_a",
        role=AgentRole.INVESTIGATOR,
        permissions=frozenset({Permission.WAREHOUSE_READ}),
    )
    now = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    assert authorize(
        identity,
        CapabilityRequest(capability=Permission.WAREHOUSE_READ, resource="query_warehouse"),
        now=now,
    ).allowed

    ledger = EvidenceLedger(RunId("run_0001"))
    ledger.record(
        source=EvidenceSource.WAREHOUSE,
        agent_id=identity.agent_id,
        request=EvidenceRequest(tool_name="query_warehouse", arguments={}, reason="r"),
        content="[]",
        collected_at=now,
    )
    assert validate_citations(make_analysis(citations=("ev_001",)), ledger).valid


def test_dry_run_cli_makes_no_network_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The P0.7 addition to the offline guarantee: the full CLI/runner path -
    argument parsing, `FakeModelClient`, the real `Investigator`, the real
    `ToolExecutor`/AuthZ path, and a real (local-file) DuckDB warehouse -
    must not open a socket. `test_no_network_access_is_attempted` above
    proves this for the P0.1-P0.3 pieces only; this proves it for the agent
    loop and CLI that P0.6/P0.7 added on top.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CAUSIQ_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CAUSIQ_RUNS_DIR", str(tmp_path / "runs"))

    def forbidden(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        msg = "the dry-run path must not open a network connection"
        raise AssertionError(msg)

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    exit_code = cli_main(["investigate", "INC-001", "--dry-run"])
    assert exit_code == 0
