# ADR-0002 — No agent framework

**Status:** Accepted
**Date:** 2026-09-08
**Requirements served:** 28 (production engineering practices), and the explainability
requirement that governs the whole project

## Context

LangChain, LlamaIndex, CrewAI, and AutoGen each offer prebuilt agent orchestration. Adopting one
would reduce the code we write for the turn loop, tool dispatch, and multi-agent coordination.

## Decision

No third-party agent orchestration framework, at any phase.

## Alternatives considered

**Adopt a framework now.** Faster to a working demo. Rejected: frameworks obscure the exact
request sent to the model, and that request is precisely the artifact under technical review.

**Adopt a framework later, at Phase 2, for multi-agent orchestration.** Rejected for the same
reason plus a migration cost: by Phase 2 the invariants (I1-I8) are load-bearing, and retrofitting
them into a framework's control flow is harder than writing the coordinator.

## Consequences

**Positive.** Every byte sent to Claude is traceable to code we wrote. The prompt-cache strategy
(Contract §6.3) and the context budget become measurable properties rather than emergent
behaviour. No version churn on a fast-moving API surface. No abstraction mismatch between
framework concepts (chains, crews, memories) and ours (evidence ledger, identity, budget).

**Negative.** More code, and no free integrations. Accepted: the value proposition of this system
is auditability, and auditability is what a framework costs us here.

**Boundary.** This ADR rejects orchestration frameworks. It does not reject libraries that do one
well-scoped job — the official Anthropic SDK, Pydantic, structlog, DuckDB, and Typer are all in
scope and justified individually in Engineering Contract §5.1.

## How we test it

Enforced by review rather than by test: a dependency added to `pyproject.toml` must name the
requirement it serves. A contract test asserts the tool-schema JSON we send is byte-stable, which
is only meaningful because we construct it ourselves.

## Review script

*"I can show you the exact bytes we send to Claude and explain why each one is there. That's a
deliberate choice, and it's why the prompt-cache strategy and the context budget are measurable
rather than emergent."*
