-- Causiq evidence substrate: the INC-001 seeded incident (ADR-0003).
--
-- Executed once, at build time, against a fresh read-write DuckDB file by
-- causiq.evidence_substrate.build_warehouse_db(). The resulting file is then
-- reopened READ-ONLY for every investigation (causiq.tools.warehouse) - this
-- script is the only code path in the project that ever writes to it.
--
-- Determinism (invariant I6): no RANDOM(), no NOW()/CURRENT_DATE/CURRENT_TIMESTAMP,
-- no UUID(). Every value is a pure function of two small integer loop variables
-- (day index, order index) and literal constants, so re-running this script
-- against a fresh file always produces byte-identical data.
--
-- The planted defect (PHASE_0_PLAN.md 2): analytics.revenue_daily aggregates
-- raw.orders WHERE status = 'COMPLETED'. On 2026-09-07 the upstream order
-- service begins emitting PENDING_CAPTURE for most orders. Total order VOLUME
-- that day is unchanged - this is a semantic filter defect, not data loss.
--
-- This file plants the RAW FACTS ONLY. It does not encode a "root cause" as
-- data - analytics.revenue_daily is populated by literally running the
-- COMPLETED-only aggregation against raw.orders, so the causal relationship
-- between the two tables is real and independently re-derivable by any query
-- that reads raw.orders, not a value asserted by fiat.

CREATE SCHEMA raw;
CREATE SCHEMA analytics;

-- --------------------------------------------------------------------------
-- raw.orders - one row per order, exactly as the upstream order service would
-- emit it. 14 days x 45 orders/day = 630 rows, 2026-08-25 through 2026-09-07
-- (the incident date is the 14th and final day, day index 13).
-- --------------------------------------------------------------------------
CREATE TABLE raw.orders (
    order_id BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL,
    order_ts TIMESTAMP NOT NULL,
    status VARCHAR NOT NULL,
    amount_usd DECIMAL(10, 2) NOT NULL
);

-- Status assignment, by day:
--   days 0..12 (2026-08-25 .. 2026-09-06, "normal"):
--     order_index % 20 = 0  -> CANCELLED   (routine background noise, 1/day)
--     otherwise             -> COMPLETED   (~95% of volume)
--   day 13 (2026-09-07, the incident date):
--     order_index % 5 = 0   -> COMPLETED         (20% - unhandled legacy path)
--     otherwise             -> PENDING_CAPTURE   (80% - the new upstream status)
--
-- amount_usd is a deterministic sawtooth over order_index so revenue is not
-- perfectly uniform per order, without introducing any randomness.
INSERT INTO raw.orders
SELECT
    d * 1000 + i                                                    AS order_id,
    (d * 1000 + i) % 200                                            AS customer_id,
    (DATE '2026-08-25' + INTERVAL (d) DAY) + INTERVAL (i * 32) MINUTE AS order_ts,
    CASE
        WHEN d = 13 THEN
            CASE WHEN i % 5 = 0 THEN 'COMPLETED' ELSE 'PENDING_CAPTURE' END
        ELSE
            CASE WHEN i % 20 = 0 THEN 'CANCELLED' ELSE 'COMPLETED' END
    END                                                              AS status,
    CAST(20.00 + (i % 15) * 3.37 AS DECIMAL(10, 2))                  AS amount_usd
FROM range(14) AS days(d), range(45) AS orders_per_day(i);

-- --------------------------------------------------------------------------
-- analytics.revenue_daily - the downstream aggregate that silently drops.
--
-- Populated by literally executing the production aggregation logic
-- (`WHERE status = 'COMPLETED' GROUP BY order_date`) against raw.orders. The
-- defect is a property of THIS FILTER, expressed here exactly as it would be
-- in the real dbt model - not a separately-authored, hand-typed set of
-- numbers that merely looks consistent.
-- --------------------------------------------------------------------------
CREATE TABLE analytics.revenue_daily (
    order_date DATE PRIMARY KEY,
    order_count BIGINT NOT NULL,
    total_revenue_usd DECIMAL(12, 2) NOT NULL
);

INSERT INTO analytics.revenue_daily
SELECT
    CAST(order_ts AS DATE) AS order_date,
    count(*)               AS order_count,
    sum(amount_usd)        AS total_revenue_usd
FROM raw.orders
WHERE status = 'COMPLETED'
GROUP BY 1
ORDER BY 1;
