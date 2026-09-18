# Causiq — Autonomous AI Reliability Engineer

Causiq investigates data and software reliability incidents autonomously: it retrieves evidence
from authorized systems, correlates it on a shared incident timeline, and produces a root-cause
analysis in which **every claim resolves to a specific recorded piece of evidence**. Remediation
is proposed, never executed without human approval, and verified afterwards.

**Status:** Phase 0 in progress. P0.1 (foundation) through P0.7 (the CLI/runner operator surface)
are complete and `hardened` — a real agent drives the full capability layer end to end, offline
and deterministically by default, producing a schema-validated, citation-checked analysis that is
persisted to disk. Nothing here is `production-ready`; see `docs/PHASE_0_PLAN.md`.

## Getting started

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
git clone <this repo> && cd CAUSIQ
uv sync --extra dev          # or: make setup   /   ./tasks.ps1 setup
```

### Quickstart: a dry-run investigation — no API key, no network

```bash
uv run causiq investigate INC-001 --dry-run
```

This drives the *real* agent loop against the *real* tool-execution and authorization path and a
real (auto-built on first use) DuckDB warehouse — only the model is a deterministic, scripted
fake, so the whole thing is free, offline, and fast on a fresh clone, on Windows or Linux.

Expected output (the run id and paths vary per invocation):

```
audit journal: var/audit/run_xxxxxxxxxxxxxxxx.jsonl
result record: var/runs/run_xxxxxxxxxxxxxxxx.json
run:       run_xxxxxxxxxxxxxxxx
incident:  inc_INC-001
state:     completed
evidence:  3 record(s)
outcome:   completed
root cause: analytics.revenue_daily aggregates only status = 'COMPLETED'. On 2026-09-07 ...
summary:   Revenue under-reported on 2026-09-07 because a new upstream order status is excluded ...
```

Add `--json` to print the full, schema-valid `InvestigationRun` instead of the human summary
(useful for piping to `jq` or a file — the artifact paths still go to stderr, so stdout stays
pure JSON):

```bash
uv run causiq investigate INC-001 --dry-run --json
```

**Where things are written.** Every invocation persists two files, named after the run's id:
`var/audit/<run_id>.jsonl` — the append-only audit trail, one JSON line per event, flushed as it
is written, so it survives a crash mid-run — and `var/runs/<run_id>.json` — the terminal
`InvestigationRun` record, including every piece of evidence collected and the analysis if any.
Both paths are printed to stderr on every invocation. `var/` is git-ignored; nothing under it is
committed.

An unknown incident id, or a live invocation with no key, fails immediately with a clear message
on stderr and exit code `2` — nothing is written, because no investigation ever started.

### Live investigation — requires `ANTHROPIC_API_KEY`

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # PowerShell: $env:ANTHROPIC_API_KEY = "sk-ant-..."
uv run causiq investigate INC-001
```

Drop `--dry-run` and Causiq calls the real Anthropic API instead of the scripted fake model.
Everything else — the tool layer, authorization, evidence collection, citation validation, and
persistence — is identical to the dry run. Causiq never reads credentials from a Claude web
session, a CLI profile, or anywhere else; without a key set, this fails clearly rather than
silently falling back to one.

### Everything else

Then run the gate. It is offline and deterministic: **no API key, no network**.

| Task | macOS / Linux | Windows |
|---|---|---|
| Everything CI runs | `make check` | `./tasks.ps1 check` |
| Tests only | `make test` | `./tasks.ps1 test` |
| Live-API tests (needs a key) | `make test-live` | `./tasks.ps1 test-live` |
| Lint + format check | `make lint` | `./tasks.ps1 lint` |
| Type check | `make type` | `./tasks.ps1 type` |
| Coverage gates | `make cov` | `./tasks.ps1 cov` |

`make` is not installed on Windows by default, which is why `tasks.ps1` exists; it runs the same
commands. Both use `uv run --no-sync`, because the working copy lives in a OneDrive-synced folder
where uv's reinstall step intermittently hits a locked directory.

Expected result: **439 passed, 3 deselected** (the deselected tests are the opt-in live suite),
100% coverage, mypy and ruff clean.

## Documents

| Document | What it is |
|---|---|
| [docs/ENGINEERING_CONTRACT.md](docs/ENGINEERING_CONTRACT.md) | The binding technical agreement: invariants, architecture, technology decisions, Claude usage contract, security model, testing contract, definition of done, and the 28-requirement traceability matrix |
| [docs/PHASE_0_PLAN.md](docs/PHASE_0_PLAN.md) | The first vertical slice: the seeded incident, work breakdown, 19 acceptance criteria, and the explicit list of what is *not* being built yet |
| [docs/architecture/](docs/architecture/) | Execution-flow and data-flow diagrams for the code as it exists today (P0.6/P0.7) |
| [docs/adr/](docs/adr/) | Architecture Decision Records 0001–0008 |

## Core invariants

1. **No claim without evidence** — every RCA assertion cites an id that exists in the run ledger.
2. **Evidence is immutable and attributable** — what was asked, what returned, by whom, when.
3. **The model never touches a system directly** — it emits intents; the harness authorizes.
4. **Read is default, write is granted** — mutation needs an identity grant *and* human approval.
5. **Every run is bounded, reproducible, auditable, and terminates in a recorded state.**

## Phase roadmap

`P0` walking skeleton · `P1` full evidence surface · `P2` orchestration and sub-agents ·
`P3` OpenTelemetry and Langfuse · `P4` evaluation and LLM-as-a-Judge · `P5` remediation,
approval and verification · `P6` MCP and network A2A · `P7` Docker, ECS, Kubernetes, serverless

Built on the Anthropic Messages API (`claude-opus-5`) with a self-owned agent loop — see
[ADR-0001](docs/adr/0001-own-the-agent-loop.md).

## Repositories

This project is maintained in two repositories with **independent commit history** and identical
content at each milestone. See [ADR-0006](docs/adr/0006-dual-repository-git-strategy.md).

| Role | Directory | Remote |
|---|---|---|
| Work | `…/OneDrive - ProductSquads Technolabs LLP/CAUSIQ` | `PS-HENIL-PATEL/L3-PROJECT` |
| Personal | `…/causiq-personal` | `henil0109/CAUSIQ-Autonomous-AI-Reliability-Engineer` |

Remote URLs embed the account username so Git Credential Manager selects the correct per-account
credential. Each repository has exactly one remote, which makes a wrong-account push structurally
impossible.

### Re-authenticating the personal remote

The stored credential for `henil0109` is expired. From an **interactive** terminal:

```powershell
# Option A — clear the stale credential and let GCM prompt on next push
cmdkey /delete:LegacyGeneric:target=git:https://henil0109@github.com
cd $env:USERPROFILE\causiq-personal
git push -u origin main          # GCM opens a browser; sign in as henil0109

# Option B — install GitHub CLI and authenticate that account
winget install --id GitHub.cli
gh auth login --hostname github.com --git-protocol https --web
```

Verify before pushing that the correct account is targeted:

```powershell
git remote -v                    # must show henil0109@github.com
```
