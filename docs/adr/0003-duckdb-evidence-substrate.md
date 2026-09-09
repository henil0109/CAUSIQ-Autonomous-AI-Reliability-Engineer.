# ADR-0003 — Local DuckDB + versioned artifacts as the evidence substrate

**Status:** Accepted
**Date:** 2026-09-08
**Requirements served:** 8 (data-quality investigation), 16, 17, 18 (evaluation), 27 (automated
testing)

## Context

Causiq investigates a data platform. Something has to play the part of that platform during
development, and the choice constrains whether the agent can be evaluated at all.

## Decision

The investigated world is a seeded DuckDB warehouse plus dbt / Airflow / git / deploy artifacts
committed to the repository, with deliberately planted defects whose true root cause is known.

## Alternatives considered

**Dockerised Postgres + real Airflow.** Highest fidelity; the pipeline failures are genuine.
Rejected for Phase 0: significant setup before any agent code exists, and non-deterministic runs
make agent evaluation much harder to score.

**A real cloud warehouse (Snowflake / BigQuery / Redshift).** Maximum realism. Rejected: requires
credentials, costs money per query, cannot run in CI, and forces the production security model to
the front of Phase 0 before the architecture has been reviewed.

## Consequences

**Positive.** DuckDB is a real SQL engine, so data-quality checks execute genuine SQL against
genuine tables — nothing about the investigation is simulated except the provenance of the data.
The artifacts are real file formats (`dbt run_results.json`, Airflow DAG-run payloads), so the
parsers we write are the parsers a production deployment needs. Because the defect is planted, the
expected root cause is known and assertable, which is the precondition for Phase 4 evaluation.
Satisfies invariant I6 (every run reproducible offline).

**Negative.** The substrate is smaller and cleaner than a real warehouse. Phase 1 mitigates by
adding distinct incident classes; realism of scale is explicitly out of scope.

**Integration path.** Each evidence source sits behind a tool. Swapping DuckDB for Snowflake in
production is a new adapter behind the same `query_warehouse` tool contract; the agent layer does
not change.

## How we test it

The seed is deterministic and verified by content digest. Integration tests execute the expected
investigation queries against it and assert result shapes.

## Review script

*"The substrate is local so the evaluation is honest. I know the true root cause of every seeded
incident, which means I can score the agent instead of admiring it."*
