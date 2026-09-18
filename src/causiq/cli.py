"""The `causiq` command-line entry point (P0.7).

Maturity: hardened.

Argparse, not a third-party CLI framework: the whole surface today is one
subcommand and two boolean flags, which argparse expresses directly. Typer
was deliberately not added for this - the Engineering Contract's dependency
policy (5.1) requires each dependency to name the requirement it serves, and
"a nicer decorator syntax for one subcommand" does not clear that bar.

This module is intentionally thin and contains no investigation logic of its
own: it parses arguments, calls `causiq.runner.run_investigation` (which owns
the actual orchestration), formats the result, and chooses an exit code.
Nothing here touches DuckDB, the evidence ledger, or a tool - `run_investigation`
and the `Investigator` it constructs own that boundary entirely (invariant I3).

Exit codes are a plain three-tier scheme, chosen for scriptability rather than
mirroring `RunState` one-for-one:

* ``0`` - the investigation reached `RunState.COMPLETED`.
* ``1`` - the investigation reached any other terminal state
  (`INCONCLUSIVE`, `BUDGET_EXCEEDED`, `FAILED`). The run still happened and its
  artifacts were still written; this code only says "no root cause was
  confirmed," which is a meaningfully different condition from...
* ``2`` - a usage or configuration error before any investigation could start
  (an unknown incident id, or a live invocation with no `ANTHROPIC_API_KEY`).
  No run artifacts exist for this case, because no run was ever attempted.

`print()` is not used for output (Engineering Contract T20 lint rule; see
`scripts/seed_warehouse.py` for the same convention) - everything goes
through `sys.stdout`/`sys.stderr` explicitly instead.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from causiq.domain import InvestigationRun, RunState
from causiq.errors import ConfigurationError
from causiq.runner import RunArtifacts, run_investigation

_EXIT_COMPLETED = 0
_EXIT_UNSUCCESSFUL = 1
_EXIT_USAGE_ERROR = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="causiq",
        description="Causiq - Autonomous AI Reliability Engineer (Phase 0)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    investigate = subparsers.add_parser(
        "investigate",
        help="Investigate one incident fixture end to end",
    )
    investigate.add_argument(
        "incident_id",
        help="Incident fixture id, e.g. INC-001",
    )
    investigate.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Use a scripted, deterministic fake model instead of the real "
            "Anthropic API - no network access, no ANTHROPIC_API_KEY required."
        ),
    )
    investigate.add_argument(
        "--json",
        action="store_true",
        help="Print the terminal InvestigationRun as JSON instead of a human summary",
    )
    return parser


def _print_human_summary(run: InvestigationRun) -> None:
    lines = [
        f"run:       {run.run_id}",
        f"incident:  {run.incident_id}",
        f"state:     {run.state}",
        f"evidence:  {len(run.evidence)} record(s)",
    ]
    if run.analysis is not None:
        lines.append(f"outcome:   {run.analysis.outcome}")
        if run.analysis.root_cause:
            lines.append(f"root cause: {run.analysis.root_cause}")
        lines.append(f"summary:   {run.analysis.summary}")
    if run.failure_reason:
        lines.append(f"failure:   {run.failure_reason}")
    sys.stdout.write("\n".join(lines) + "\n")


def _print_artifact_paths(artifacts: RunArtifacts) -> None:
    """Always on stderr, in both output modes - keeps `--json` stdout pure
    JSON (pipeable to `jq` or a file) while still telling the operator where
    the persisted record and audit journal landed (AC-9)."""
    sys.stderr.write(f"audit journal: {artifacts.audit_path}\n")
    sys.stderr.write(f"result record: {artifacts.result_path}\n")


def _exit_code_for(run: InvestigationRun) -> int:
    return _EXIT_COMPLETED if run.state is RunState.COMPLETED else _EXIT_UNSUCCESSFUL


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the `causiq` console script and for direct testing.

    Deliberately does not catch anything beyond the two documented,
    anticipated failure modes below - an unexpected exception (a genuine bug)
    is allowed to propagate as a traceback rather than being absorbed into a
    generic error message that would hide it.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        artifacts = run_investigation(args.incident_id, dry_run=args.dry_run)
    except FileNotFoundError:
        sys.stderr.write(f"causiq: unknown incident {args.incident_id!r}\n")
        return _EXIT_USAGE_ERROR
    except ConfigurationError as exc:
        # Never the exception's raw args in case a future ConfigurationError
        # subclass ever carried something sensitive in `context` - `str(exc)`
        # is this taxonomy's own deliberately-safe rendering (causiq.errors).
        sys.stderr.write(f"causiq: {exc}\n")
        return _EXIT_USAGE_ERROR

    run = artifacts.run
    if args.json:
        sys.stdout.write(run.model_dump_json(indent=2) + "\n")
    else:
        _print_human_summary(run)
    _print_artifact_paths(artifacts)

    return _exit_code_for(run)


if __name__ == "__main__":  # pragma: no cover - trivial process entry, not importable to exercise
    raise SystemExit(main())
