"""`causiq.cli` - argument parsing, output formatting, and exit codes (P0.7).

Every test here calls `causiq.cli.main()` in-process (never a subprocess),
and redirects `Settings.audit_dir`/`runs_dir` to a temp directory via the
`CAUSIQ_AUDIT_DIR`/`CAUSIQ_RUNS_DIR` environment variables - the same
mechanism `load_settings()` reads in real use - so these tests never touch
the repository's real `var/` directory. Only `--dry-run` is exercised for
success paths; the CLI never needs a network call to be fully tested.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from causiq.cli import _EXIT_COMPLETED, _EXIT_USAGE_ERROR, _print_human_summary, main
from causiq.domain import (
    Analysis,
    AnalysisOutcome,
    Budget,
    Confidence,
    InvestigationRun,
    RunState,
)
from causiq.ids import IncidentId, RunId
from tests.conftest import T0

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_settings_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file gets its own audit/runs directories and no
    API key, unless a test explicitly sets one."""
    monkeypatch.setenv("CAUSIQ_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CAUSIQ_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_dry_run_prints_a_human_summary_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["investigate", "INC-001", "--dry-run"])

    assert exit_code == _EXIT_COMPLETED
    out = capsys.readouterr()
    assert "state:     completed" in out.out
    assert "root cause:" in out.out
    assert "audit journal:" in out.err
    assert "result record:" in out.err


def test_dry_run_json_prints_schema_valid_run_and_paths_stay_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["investigate", "INC-001", "--dry-run", "--json"])

    assert exit_code == _EXIT_COMPLETED
    out = capsys.readouterr()
    payload = json.loads(out.out)  # must be pure JSON - no summary text mixed in
    assert payload["state"] == "completed"
    assert payload["incident_id"] == "inc_INC-001"
    assert len(payload["evidence"]) == 3
    assert "audit journal:" in out.err
    assert "result record:" in out.err


def test_json_output_matches_the_persisted_result_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    exit_code = main(["investigate", "INC-001", "--dry-run", "--json"])
    assert exit_code == _EXIT_COMPLETED
    stdout_payload = json.loads(capsys.readouterr().out)

    result_files = list((tmp_path / "runs").glob("*.json"))
    assert len(result_files) == 1
    disk_payload = json.loads(result_files[0].read_text(encoding="utf-8"))
    assert stdout_payload == disk_payload


def test_unknown_incident_is_a_clean_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["investigate", "DOES-NOT-EXIST", "--dry-run"])

    assert exit_code == _EXIT_USAGE_ERROR
    err = capsys.readouterr().err
    assert "unknown incident" in err
    assert "DOES-NOT-EXIST" in err


def test_live_without_api_key_is_a_clean_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["investigate", "INC-001"])  # no --dry-run, no key in the environment

    assert exit_code == _EXIT_USAGE_ERROR
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
    # The one thing this message must never do is echo a key - there isn't
    # one here, but the assertion pins the message shape so a future edit
    # that started interpolating `settings.anthropic_api_key` would be caught
    # by a reviewer, not just by this test happening to have no key set.
    assert "sk-ant" not in err


def test_unsuccessful_state_exits_nonzero_while_still_writing_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A budget-exceeded run is not an exception - it is a normal, if
    unsuccessful, terminal outcome, and the CLI must say so via exit code
    without losing the artifacts (AC-9)."""
    monkeypatch.setenv("CAUSIQ_BUDGET_MAX_TURNS", "1")

    exit_code = main(["investigate", "INC-001", "--dry-run", "--json"])

    assert exit_code != _EXIT_COMPLETED
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == RunState.BUDGET_EXCEEDED.value
    assert len(payload["evidence"]) == 1

    result_files = list((tmp_path / "runs").glob("*.json"))
    assert len(result_files) == 1


def test_human_summary_covers_a_run_with_no_analysis(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A budget-exceeded run has `analysis is None` and a `failure_reason` -
    the human summary's two conditional lines (`outcome`/`root cause` and
    `failure`) must both be exercised, not just the COMPLETED happy path."""
    monkeypatch.setenv("CAUSIQ_BUDGET_MAX_TURNS", "1")

    exit_code = main(["investigate", "INC-001", "--dry-run"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "state:     budget_exceeded" in out
    assert "outcome:" not in out  # no analysis was ever produced
    assert "failure:" in out


def test_human_summary_covers_an_inconclusive_analysis_with_no_root_cause() -> None:
    """An INCONCLUSIVE analysis has `root_cause is None` by construction
    (the domain model forbids stating one) - the summary must render cleanly
    without the `root cause:` line in that case. Drives `_print_human_summary`
    directly against a hand-built run, since that is what this test is about,
    rather than needing a scripted investigation to reach the same shape."""
    run = InvestigationRun(
        run_id=RunId("run_test"),
        incident_id=IncidentId("inc_TEST"),
        agent_id="agent_investigator_0",
        state=RunState.INCONCLUSIVE,
        budget=Budget(max_turns=1, max_tool_calls=1, max_total_tokens=1000, deadline_seconds=1),
        started_at=T0,
        ended_at=T0,
        analysis=Analysis(
            outcome=AnalysisOutcome.INCONCLUSIVE,
            summary="not enough signal",
            limitations="insufficient evidence",
            confidence=Confidence.LOW,
        ),
    )

    with contextlib.redirect_stdout(io.StringIO()) as buffer:
        _print_human_summary(run)

    out = buffer.getvalue()
    assert "outcome:   inconclusive" in out
    assert "root cause:" not in out
    assert "summary:   not enough signal" in out


def test_argument_parsing_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_argument_parsing_requires_an_incident_id() -> None:
    with pytest.raises(SystemExit):
        main(["investigate"])


def test_dry_run_and_json_flags_are_independently_optional(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--json` without `--dry-run` is a valid combination (live mode with
    JSON output) - argument parsing must not couple the two flags together.
    This test exercises the *parser* only: no key is set, so it still exits
    as a usage error, but via the live/no-key path, not an argparse error."""
    exit_code = main(["investigate", "INC-001", "--json"])
    assert exit_code == _EXIT_USAGE_ERROR
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
