# Execution flow (P0.6 / P0.7, as built)

**Status:** describes the code as it exists at the end of P0.7. This is not a
target architecture diagram - see `docs/PHASE_0_PLAN.md` §5 for what is
explicitly deferred, and the note at the bottom of this file for how later
phases are expected to extend it.

## One investigation, start to finish

```mermaid
sequenceDiagram
    participant CLI as causiq.cli (P0.7)
    participant Runner as causiq.runner (P0.7)
    participant Inv as Investigator
    participant Model as ModelClient
    participant Reg as ToolRegistry
    participant Exec as ToolExecutor
    participant AuthZ as authz.authorize
    participant Tool as QueryWarehouseTool
    participant Ledger as EvidenceLedger
    participant Journal as AuditJournal

    CLI->>Runner: run_investigation(incident_id, dry_run)
    Runner->>Runner: load_incident_by_id (fixture)
    Runner->>Runner: build ModelClient (Fake or Anthropic)
    Runner->>Inv: investigate(incident, budget, audit_sink)
    Inv->>Journal: RUN_STARTED

    loop until final analysis or budget exhausted
        Inv->>Inv: BudgetTracker.begin_turn()
        Inv->>Model: investigate(system, tools, history)
        alt model requests tool(s)
            Model-->>Inv: ModelTurn(tool_calls=[...])
            loop for each tool_call in this turn
                Inv->>Inv: BudgetTracker.begin_tool_call()
                Inv->>Exec: execute(ToolRequest)
                Exec->>Reg: require(tool_name)
                Exec->>AuthZ: authorize(identity, capability)
                AuthZ-->>Exec: AuthorizationDecision
                alt authorized
                    Exec->>Tool: run(validated input)
                    Tool-->>Exec: ToolOutput
                    Exec->>Ledger: record(...)
                    Ledger-->>Exec: Evidence (evidence_id minted here)
                    Exec->>Journal: TOOL_AUTHORIZED, TOOL_EXECUTED, EVIDENCE_RECORDED
                    Exec-->>Inv: ToolResult (success, evidence_id)
                else denied / unknown / invalid / failed / timeout
                    Exec->>Journal: TOOL_DENIED / TOOL_UNKNOWN / TOOL_INPUT_REJECTED / TOOL_FAILED
                    Exec-->>Inv: ToolResult (is_error=true, no evidence)
                end
            end
            Inv->>Inv: append ToolResultBlock(s) to history
        else model concludes
            Model-->>Inv: ModelTurn(analysis=Analysis)
            Inv->>Ledger: validate_citations(analysis, ledger)
            alt every citation resolves
                Inv->>Journal: ANALYSIS_VALIDATED
            else a citation is fabricated
                Inv->>Journal: ANALYSIS_REJECTED
                Note over Inv: run terminates FAILED, analysis kept for forensics only
            end
        end
    end

    Inv->>Journal: RUN_ENDED
    Inv-->>Runner: InvestigationRun (terminal state + evidence + analysis)
    Runner->>Runner: write InvestigationRun to {runs_dir}/{run_id}.json
    Runner-->>CLI: RunArtifacts (run, audit_path, result_path)
    CLI->>CLI: print summary or JSON, choose exit code
```

## What each box actually is

| Box | Module | Notes |
|---|---|---|
| `causiq.cli` | `src/causiq/cli.py` | Argument parsing, output formatting, exit code. No investigation logic. |
| `causiq.runner` | `src/causiq/runner.py` | Chooses the `ModelClient`, assembles the registry/identity, calls `Investigator`, persists the result. |
| `Investigator` | `src/causiq/agents/investigator.py` | Owns the bounded loop. The only caller of `ModelClient`, `ToolExecutor`, and `validate_citations`. |
| `ModelClient` | `src/causiq/llm/ports.py` (+ `fake_client.py`, `anthropic_client.py`) | Provider-neutral port; exactly one adapter imports the `anthropic` SDK. |
| `ToolRegistry` / `ToolExecutor` | `src/causiq/tools/` | The single path from a model's intent to a real system call (invariant I3). |
| `authz.authorize` | `src/causiq/authz/decision.py` | The only place an authorization decision is made. |
| `QueryWarehouseTool` | `src/causiq/tools/warehouse.py` | The one registered tool: read-only DuckDB access. |
| `EvidenceLedger` | `src/causiq/evidence/ledger.py` | The only code path that can mint evidence - `Tool.run()` itself has no ledger parameter. |
| `AuditJournal` | `src/causiq/audit/journal.py` | Append-only, sequenced, flushed on every write. |

## What is deliberately not in this diagram

Per `docs/PHASE_0_PLAN.md` §5, none of the following exist yet, and this
diagram does not pretend they do: a second tool, a second incident, an
orchestrator or any multi-agent structure, MCP, OpenTelemetry/Langfuse
exporters (the `Tracer` port and span names exist from P0.4, but nothing
exports them), an evaluation harness, remediation execution, or any
deployment target. Each is deferred to a named later phase.
