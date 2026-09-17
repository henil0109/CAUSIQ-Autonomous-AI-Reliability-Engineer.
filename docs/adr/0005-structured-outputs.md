# ADR-0005 — Structured outputs and strict tools for machine-consumed output

**Status:** Accepted
**Date:** 2026-09-08
**Requirements served:** 9 (evidence-backed root-cause analysis), 15-18 (evaluation), 22 (failure
handling)

## Context

Requirement 9 says the root-cause analysis must be evidence-backed. If the analysis is prose, that
property is unenforceable: there is nothing to validate, and every downstream consumer — the audit
trail, the Phase 4 judge, the Phase 5 remediation planner — has to parse natural language.

## Decision

Any model output that another component consumes is produced under a JSON Schema constraint. Free
text is only ever for human display, and only inside a schema-defined field.

Concretely:

- Analyses are produced with `client.messages.parse(..., output_format=<PydanticModel>)`, read
  from `response.parsed_output`; raw `output_config={"format": {"type": "json_schema", ...}}` where
  a Pydantic model is not the natural source.
- Every tool definition carries `strict: True`, `additionalProperties: False`, and a complete
  `required` list.
- Tool inputs are always parsed as JSON, never string-matched — escaping inside `tool_use.input`
  is not stable across models.

## Alternatives considered

**Prose output plus a parser.** Rejected: parsing prose is the largest source of silent failure in
LLM systems, and failures are semantic rather than structural, so they are hard to detect.

**Prompt-only instruction to emit JSON.** Rejected: a prompt instruction is a request, not a
guarantee. It degrades under long context, unusual inputs, and model changes.

## Consequences

**Positive.** Generation is constrained to the schema, so validation failures become rare and
structural. Combined with `validate_citations()`, this turns "evidence-backed" from a prompt
instruction into a checked property. The same schema feeds the Phase 4 evaluator, so scoring does
not need its own parser.

**Negative.** A rigid schema can force a bad analysis into a shape that looks confident. Mitigated
by invariant I8: `INCONCLUSIVE` is a first-class outcome and the schema carries a `limitations`
field, so the model has an honest exit.

**Layering.** Three independent layers protect the same property: the prompt asks for citations,
the schema requires them, and the validator proves them.

## How we test it

Contract tests assert the generated JSON Schema is stable across commits (a change must be
deliberate). Unit tests assert that an analysis with a fabricated citation is rejected. A live-API
test asserts a real call returns a schema-valid parsed object.

## Review script

*"The prompt asks for citations. The schema requires them. The validator proves them. Three
layers, because a prompt instruction alone is a request, not a guarantee."*

## Amendment (2026-09-17, during P0.6)

The delivered adapter (`llm/anthropic_client.py`) calls `client.messages.create(...)` with
`output_config={"format": {"type": "json_schema", "schema": Analysis.model_json_schema()}}` and
validates the returned text with `Analysis.model_validate_json(...)` directly, rather than
`client.messages.parse(..., output_format=Analysis)` as sketched above. The two put an identical
request on the wire; see ADR-0008 for why `create()` was chosen once a real adapter existed to
check the alternative against. The property this ADR actually decided - that machine-consumed
model output is schema-constrained end to end, not prose plus a parser - holds exactly as
written.
