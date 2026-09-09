# ADR-0001 — Own the agent loop on the Messages API

**Status:** Accepted
**Date:** 2026-09-08
**Requirements served:** 1, 6, 19, 20, 21, 22, 23, 24, 25, 26

## Context

Causiq needs an agent loop: something that decides when to call the model, what context to send,
which tool calls to permit, and when to stop. There are four ways to obtain one — a manual loop
on the Messages API, the Anthropic SDK's beta `tool_runner` helper, the Claude Agent SDK, or
Anthropic's Managed Agents. The choice determines who owns the loop and who owns the deployment.

## Decision

Build the loop directly on the Anthropic Python SDK's `client.messages.create(...)`, in our own
process, on our own infrastructure. Do not use the Claude Agent SDK, Managed Agents, or the beta
`tool_runner`.

## Alternatives considered

**Claude Agent SDK.** Ships the Claude Code harness as a library: built-in Read/Write/Edit/Bash/
Grep tools, subagents, hooks, permissions, sessions. Rejected because its built-in tool surface is
filesystem- and coding-shaped, which is the wrong shape for data-reliability evidence, and because
the harness would be the thing we explain in review rather than the thing we authored.

**Managed Agents.** Anthropic runs the loop and hosts a per-session sandbox; agents are persisted,
versioned server objects. Genuinely the simplest option for a hosted stateful agent, and the right
answer for a different project. Rejected here because it removes requirements 23–26 (Docker,
serverless, ECS, Kubernetes) from the project entirely — the deployment work is part of what this
project exists to demonstrate.

**SDK `tool_runner` helper.** Automates the request-execute-loop cycle over tools we define, with
per-turn hooks. Rejected because the loop is roughly sixty lines, and writing it removes a beta
dependency while giving us the exact interception point where authorization, evidence recording,
and audit writes must happen. The helper's hooks could host those, but the code we would write is
the same size and less direct.

## Consequences

**Positive.** Requirements 19–22 (agent identity, tool permission controls, auditability, failure
handling) become implementable, because every tool call passes through one function we own.
Requirements 23–26 stay in scope, because the loop runs on our infrastructure. No beta API
dependency in the critical path.

**Negative.** We write and maintain the loop, including `stop_reason` handling for `end_turn`,
`tool_use`, `max_tokens`, `pause_turn`, and `refusal`, plus retry classification. This is accepted
cost.

**Integration path.** The loop sits behind the `ModelClient` port (ADR-0004), so the transport can
later move to `AnthropicBedrockMantle` for AWS, or a Vertex/Foundry client, without touching
orchestration code.

## How we test it

Scripted `FakeModelClient` responses covering every `stop_reason`, multi-block parallel tool use,
budget exhaustion, and transient-error retry — all offline and deterministic. A small live-API
suite, skipped without a key, proves the real adapter agrees with the fake.

## Review script

*"We own the loop because the loop is where the security model lives. Every tool call passes
through one function that checks identity and permission, executes, records evidence with
provenance, and writes an audit entry. If a framework owned that loop, I could not show you that
function."*
