# Architecture Decision Records

Numbered records of decisions that would be expensive to reverse. Per the Engineering Contract
§13, a decision recorded here is changed by writing a superseding ADR, not by quietly diverging
in code.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-own-the-agent-loop.md) | Own the agent loop on the Messages API | Accepted |
| [0002](0002-no-agent-framework.md) | No agent framework | Accepted |
| [0003](0003-duckdb-evidence-substrate.md) | Local DuckDB + versioned artifacts as the evidence substrate | Accepted |
| [0004](0004-ports-in-phase-0.md) | Define model, tracing, audit and clock ports in Phase 0 | Accepted |
| [0005](0005-structured-outputs.md) | Structured outputs and strict tools for machine-consumed output | Accepted |
| [0006](0006-dual-repository-git-strategy.md) | Two independent repositories with independent commit history | Accepted |
| [0007](0007-sql-read-only-defense-in-depth.md) | Defense in depth for read-only SQL execution (`query_warehouse`) | Accepted |
| [0008](0008-bounded-agent-loop-and-structured-output-call-shape.md) | Bounded agent loop, and calling the Messages API directly rather than `messages.parse` | Accepted |
| [0009](0009-artifact-evidence-sources.md) | Artifact evidence sources: one Tool per source, no new abstraction layer | Accepted |

## Format

Each record carries: Context, Decision, Alternatives considered, Consequences, How we test it,
and a Review script — the last being how to explain the decision when challenged in a technical
review.
