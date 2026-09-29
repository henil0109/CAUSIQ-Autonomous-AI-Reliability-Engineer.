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

## Amendment (2026-09-29, during P1.3)

`INC-002` needed a freshness fact `analytics.revenue_daily` had no way to express (it carries no
load-timestamp column). Rather than add one to that table, `fixtures/warehouse/seed.sql` gained a
second, fully additive table, `analytics.model_refresh_log(model_name, order_date, refreshed_at)`
- `raw.orders` and `analytics.revenue_daily` are untouched.

This table is deliberately **not** computed from, generated from, or kept in sync with the dbt
run-results fixture. It is an independently authored warehouse-side observation, planted by hand
exactly like `raw.orders` - a plausible fact the warehouse itself would track (when did each
model's output last receive a successful write), asserted on its own terms. It happens to show a
gap for `daily_revenue_pipeline` on 2026-09-05 and for `customer_ltv` on 2026-09-07. Whether that
gap aligns with what the Airflow or dbt evidence separately shows is exactly the correlation
`INC-002`'s investigation is meant to discover by reasoning across independent evidence sources -
not a relationship this ADR's fixtures assert or encode on the investigation's behalf. Keeping the
sources independent this way is what makes the multi-source correlation in `INC-002` a real test
of the Investigator's reasoning rather than a fact the fixture author already worked out and wrote
down twice. See ADR-0009's P1.3 amendment for the DQ/dbt/Airflow tool side of `INC-002`.
