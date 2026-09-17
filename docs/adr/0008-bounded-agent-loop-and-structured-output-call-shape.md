# ADR-0008 — Bounded agent loop, and calling the Messages API directly rather than `messages.parse`

**Status:** Accepted
**Date:** 2026-09-17
**Requirements served:** 9 (evidence-backed RCA), invariants I1, I3, I4, I5

## Context

P0.6 delivers the first component that actually calls a model: `agents/investigator.py` (the
bounded loop) and `llm/anthropic_client.py` (the adapter). Two design questions came up during
implementation that the speculative plan in `ENGINEERING_CONTRACT.md` §6 and ADR-0005 had not
resolved against a real SDK, and both were settled empirically against `anthropic` 1.6.0 rather
than assumed - the same discipline ADR-0007 used for DuckDB.

**Question 1 — does `Investigator.investigate()` retry a transient model failure?**
Engineering Contract §6.5 and §8 describe recoverable errors as getting "a bounded retry."

**Question 2 — should the adapter call `client.messages.parse(..., output_format=Analysis)`, as
ADR-0005 sketched, or `client.messages.create(...)` with a manually supplied `output_config` and
manual `Analysis.model_validate_json(...)`?**

## Decision

### The loop is single-attempt per turn; retry is out of scope for P0.6

`Investigator._call_model()` catches `ModelRefusalError`, `ModelContractError`, and
`ModelError` (which includes `ModelTransientError`) and terminates the run as `FAILED` on all
three - none are retried. This is a deliberate P0.6 scope cut, not an oversight:

- A retry policy needs a place to live (attempt counters, backoff, what counts against the
  turn/tool-call budget) that does not yet have a settled design, and building it against a
  fake adapter that cannot produce a real rate limit is more likely to encode wrong assumptions
  than to prove anything.
- Every budget in `Budget`/`BudgetTracker` (P0.3) already bounds the loop without it: a run
  that fails fast on a transient error is strictly safer than one that is silently unbounded
  while "retrying."
- The existing error taxonomy (`errors.py`) still classifies `ModelTransientError` as
  recoverable in the abstract sense (it carries `retry_after` from `RateLimitError`); P0.6 just
  does not act on that classification yet. A future phase adding retry changes
  `_call_model()`'s `except ModelError` branch and nothing else.

This is recorded here rather than silently narrowing Contract §6.5's language, per Engineering
Contract 13.

### The adapter calls `create()` directly; it does not use `messages.parse`

`AnthropicModelClient._create()` calls `client.messages.create(..., output_config={"effort":
..., "format": {"type": "json_schema", "schema": Analysis.model_json_schema()}})` and
`_parse_response()` manually extracts `tool_use` blocks or validates a `text` block against
`Analysis` with `Analysis.model_validate_json(...)`.

This was checked against the installed SDK before being decided, not assumed:

- `Messages.parse(..., output_format=Analysis)` is sugar over exactly this request body -
  reading its source (`anthropic.resources.messages.messages.Messages.parse`) shows it merges
  `output_format` into `output_config` and posts the identical JSON, then runs a post-parser
  (`parse_response`) that fills in `.parsed_output` on any `text` content block and passes
  `tool_use` blocks through untouched.
- So the two approaches are wire-identical; the choice is purely about which side owns error
  handling. `investigate()` needs to classify five outcomes (`tool_use`, a schema-valid final
  answer, a refusal, an incomplete response, and an unsupported `stop_reason`) with a Causiq
  error type for each. Doing that against the raw `Message` is one dispatch in one function;
  doing it against a `ParsedMessage[Analysis]` would still require re-inspecting `stop_reason`
  and content blocks by hand for the four non-parsed cases, so the convenience wrapper buys
  nothing here and would add a second SDK type (`ParsedMessage`) to keep isolated inside the
  adapter for no benefit.
- One consequence worth naming: because Causiq calls `Analysis.model_validate_json` itself
  rather than through the SDK's `TypeAdapter`-based path, a future change to `transform_schema`
  behaviour inside the SDK (which shapes what schema is actually sent) cannot silently change
  how the response is parsed on the way back - the two directions are decoupled, which is a
  minor additional argument in the same direction, not the original reason for the choice.

ADR-0005's illustrative code (`client.messages.parse(..., output_format=<PydanticModel>)`)
described the intent - constrained, schema-validated output - correctly; this ADR corrects only
the specific call shape once a real adapter existed to check it against.

### `pause_turn` is not handled

Engineering Contract §6.4 lists `pause_turn` as a `stop_reason` to resume from. P0.6 offers the
model exactly one tool (`query_warehouse`), which is not a long-running server-side tool, so
`pause_turn` cannot currently occur; `_parse_response()` has no branch for it and would raise
`ModelContractError` ("unsupported stop_reason") if it ever appeared. Handling it is deferred
until a tool exists that can actually produce it.

## Consequences

**Positive.** The adapter's error handling is one function, easy to review against the taxonomy
in Contract §8; the loop's termination behaviour (fail fast, let the budget be the only source
of "how long do we keep trying") is simple enough to state and test exhaustively (P0.6 negative
tests A-G).

**Negative.** A transient 500 or rate limit ends the run today, even though the SDK's own
transport-level retry (`anthropic.Anthropic`'s default 2 retries) already absorbs the most
common transient cases before Causiq ever sees them - so this mostly matters for the failures
that survive the transport retry. Acceptable for P0.6; revisit once a retry design exists.

## How we test it

`tests/unit/test_anthropic_client.py` constructs real `anthropic.RateLimitError`,
`InternalServerError`, and `BadRequestError` instances (via real `httpx2.Request`/`Response`
objects, not hand-rolled substitutes) and asserts the exact `ModelError` subtype each maps to.
`tests/unit/test_investigator.py` (negative tests C/E/F/G) proves a transient error, a malformed
response, and budget exhaustion all terminate the run without a retry and without fabricating a
conclusion.

## Review script

*"The loop doesn't retry anything in P0.6 - a transient failure ends the run, and the budget is
what keeps the whole thing bounded regardless. And the adapter calls `create()`, not `parse()`,
even though ADR-0005 sketched `parse()` - I checked the SDK source, they send the same request
body, and `create()` gives me one place to classify five different response shapes instead of
two."*
