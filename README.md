# Causiq — Autonomous AI Reliability Engineer

Causiq investigates data and software reliability incidents autonomously: it retrieves evidence
from authorized systems, correlates it on a shared incident timeline, and produces a root-cause
analysis in which **every claim resolves to a specific recorded piece of evidence**. Remediation
is proposed, never executed without human approval, and verified afterwards.

**Status:** Phase 0 in progress. P0.1 (foundation), P0.2 (config / logging / errors / ports)
and P0.3 (domain model + evidence ledger) are complete and `hardened`. The agent loop, the
`query_warehouse` tool, and the Anthropic adapter arrive in P0.4-P0.7.

## Getting started

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
git clone <this repo> && cd CAUSIQ
uv sync --extra dev          # or: make setup   /   ./tasks.ps1 setup
```

Then run the gate. It is offline and deterministic: **no API key, no network**.

| Task | macOS / Linux | Windows |
|---|---|---|
| Everything CI runs | `make check` | `./tasks.ps1 check` |
| Tests only | `make test` | `./tasks.ps1 test` |
| Lint + format check | `make lint` | `./tasks.ps1 lint` |
| Type check | `make type` | `./tasks.ps1 type` |
| Coverage gates | `make cov` | `./tasks.ps1 cov` |

`make` is not installed on Windows by default, which is why `tasks.ps1` exists; it runs the same
commands. Both use `uv run --no-sync`, because the working copy lives in a OneDrive-synced folder
where uv's reinstall step intermittently hits a locked directory.

Expected result: **178 passed**, 100% coverage, mypy and ruff clean.

## Documents

| Document | What it is |
|---|---|
| [docs/ENGINEERING_CONTRACT.md](docs/ENGINEERING_CONTRACT.md) | The binding technical agreement: invariants, architecture, technology decisions, Claude usage contract, security model, testing contract, definition of done, and the 28-requirement traceability matrix |
| [docs/PHASE_0_PLAN.md](docs/PHASE_0_PLAN.md) | The first vertical slice: the seeded incident, work breakdown, 19 acceptance criteria, and the explicit list of what is *not* being built yet |
| [docs/adr/](docs/adr/) | Architecture Decision Records 0001–0006 |

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
