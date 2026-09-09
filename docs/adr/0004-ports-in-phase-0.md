# ADR-0004 — Define model, tracing, audit and clock ports in Phase 0

**Status:** Accepted
**Date:** 2026-09-08
**Requirements served:** 13, 14 (observability), 21 (auditability), 27 (automated testing)

## Context

Phase 0 needs to call Claude, persist audit records, and produce deterministic timestamps.
Phase 3 needs to emit OpenTelemetry spans and Langfuse traces from inside the agent loop.
Retrofitting observability and testability into an agent loop after the fact is expensive and
usually done badly, because it means editing every call site.

## Decision

Define four protocols in Phase 0, each with a real implementation and a test implementation:

| Port | Phase 0 real implementation | Phase 0 test implementation | Why it earns its place now |
|---|---|---|---|
| `ModelClient` | Anthropic SDK adapter | `FakeModelClient` (scripted responses) | The only way the loop is testable offline (I6) |
| `AuditSink` | Append-only JSONL journal | In-memory sink | Invariant I7 |
| `Clock` | System UTC clock | `FrozenClock` | Makes timestamps assertable and audit journals byte-reproducible (I6) |
| `Tracer` | No-op | No-op | Fixes the span vocabulary so Phase 3 adds an adapter, not call sites |

The span vocabulary is fixed now even though nothing emits it: `causiq.run`, `causiq.agent.turn`,
`causiq.model.call`, `causiq.tool.authorize`, `causiq.tool.execute`, `causiq.evidence.record`,
`causiq.analysis.validate`.

## Alternatives considered

**Import the SDKs directly at each call site and refactor at Phase 3.** Simpler now. Rejected:
it makes the Phase 0 test suite depend on an API key, violating I6 from the first commit, and
turns Phase 3 into a cross-cutting refactor of the most security-sensitive code in the system.

**Define a general plugin/registry system for adapters.** Rejected as speculative generality.
Each port has exactly two implementations and no discovery mechanism.

## Consequences

**Positive.** The default test suite runs offline, deterministically, with no API key and no
network. Phase 3 is an adapter plus configuration. Phase 7's move to a Bedrock or Vertex client is
a `ModelClient` implementation, not an orchestration change.

**Negative.** Four indirections that a reader has to follow. Mitigated by keeping each protocol
small — a handful of methods — and by the fact that each has a concrete Phase 0 consumer.

**Guard against drift.** A port with only one implementation and no test double is a smell; if that
happens, the port should be deleted and the concrete type used directly.

## How we test it

Each port has a conformance test that both implementations must pass. `FrozenClock` determinism is
asserted by running the same investigation twice and diffing the audit journals byte for byte.

## Review script

*"I added four interfaces, not a plugin system. Each has exactly two implementations — the real one
and the test one — and each earns its place in Phase 0. The tracer is the interesting one: it does
nothing today, but the span names are fixed, so adding Langfuse in Phase 3 touches one file
instead of every call site in the agent loop."*
