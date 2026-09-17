"""The evidence substrate builder - invariant I6, "reproducible offline".

`build_warehouse_db` is the one function in the project allowed to write to
the warehouse file. These tests are about that write path and the fixtures it
consumes; `causiq.tools.warehouse` (read-only) is tested separately.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb
import pytest

from causiq.domain import Incident, Severity
from causiq.evidence_substrate import (
    DEFAULT_INCIDENT_DIR,
    DEFAULT_SEED_SQL_PATH,
    build_warehouse_db,
    load_incident,
    load_incident_by_id,
)

pytestmark = pytest.mark.unit


def _digest_of(db_path: Path) -> str:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        orders = con.execute("SELECT * FROM raw.orders ORDER BY order_id").fetchall()
        revenue = con.execute(
            "SELECT * FROM analytics.revenue_daily ORDER BY order_date"
        ).fetchall()
    finally:
        con.close()
    return hashlib.sha256(f"{orders!r}{revenue!r}".encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Deterministic fixture creation (I6)
# --------------------------------------------------------------------------- #
def test_two_independent_builds_produce_byte_identical_data(tmp_path: Path) -> None:
    """The same seed must produce the same database state - no RANDOM(), no
    NOW(), no UUID() anywhere in seed.sql."""
    first = tmp_path / "a.duckdb"
    second = tmp_path / "b.duckdb"
    build_warehouse_db(first, seed_sql_path=DEFAULT_SEED_SQL_PATH)
    build_warehouse_db(second, seed_sql_path=DEFAULT_SEED_SQL_PATH)
    assert _digest_of(first) == _digest_of(second)


def test_seed_sql_contains_no_nondeterministic_functions() -> None:
    """Belt-and-braces: assert the banned functions are simply absent from the
    fixture's SQL (comment lines excluded, since this file's own docstring-style
    header names every one of them while explaining why they are absent), so a
    future edit that reintroduces one fails immediately rather than only
    showing up as a flaky digest mismatch."""
    lines = DEFAULT_SEED_SQL_PATH.read_text(encoding="utf-8").splitlines()
    sql_only = "\n".join(line for line in lines if not line.strip().startswith("--")).upper()
    for forbidden in ("RANDOM(", "UUID(", "NOW(", "CURRENT_TIMESTAMP", "CURRENT_DATE"):
        assert forbidden not in sql_only, forbidden


def test_build_refuses_to_overwrite_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "warehouse.duckdb"
    build_warehouse_db(path, seed_sql_path=DEFAULT_SEED_SQL_PATH)
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        build_warehouse_db(path, seed_sql_path=DEFAULT_SEED_SQL_PATH)


def test_build_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "warehouse.duckdb"
    build_warehouse_db(path, seed_sql_path=DEFAULT_SEED_SQL_PATH)
    assert path.exists()


# --------------------------------------------------------------------------- #
# Expected tables and seeded data exist
# --------------------------------------------------------------------------- #
def test_expected_tables_exist(warehouse_db_path: Path) -> None:
    con = duckdb.connect(str(warehouse_db_path), read_only=True)
    try:
        tables = {
            (row[0], row[1])
            for row in con.execute(
                "SELECT table_schema, table_name FROM information_schema.tables"
            ).fetchall()
        }
    finally:
        con.close()
    assert ("raw", "orders") in tables
    assert ("analytics", "revenue_daily") in tables


def test_orders_table_has_the_expected_row_count(warehouse_db_path: Path) -> None:
    """14 days x 45 orders/day."""
    con = duckdb.connect(str(warehouse_db_path), read_only=True)
    try:
        (count,) = con.execute("SELECT count(*) FROM raw.orders").fetchone()  # type: ignore[misc]
    finally:
        con.close()
    assert count == 630


def test_revenue_daily_has_fourteen_days(warehouse_db_path: Path) -> None:
    con = duckdb.connect(str(warehouse_db_path), read_only=True)
    try:
        (count,) = con.execute("SELECT count(*) FROM analytics.revenue_daily").fetchone()  # type: ignore[misc]
    finally:
        con.close()
    assert count == 14


def test_incident_date_shows_pending_capture_and_prior_days_do_not(
    warehouse_db_path: Path,
) -> None:
    con = duckdb.connect(str(warehouse_db_path), read_only=True)
    try:
        before = con.execute(
            "SELECT count(*) FROM raw.orders "
            "WHERE status = 'PENDING_CAPTURE' AND order_ts < DATE '2026-09-07'"
        ).fetchone()
        on_incident_day = con.execute(
            "SELECT count(*) FROM raw.orders "
            "WHERE status = 'PENDING_CAPTURE' AND CAST(order_ts AS DATE) = DATE '2026-09-07'"
        ).fetchone()
    finally:
        con.close()
    assert before == (0,)
    assert on_incident_day is not None and on_incident_day[0] > 0


# --------------------------------------------------------------------------- #
# INC-001 incident fixture
# --------------------------------------------------------------------------- #
def test_inc001_fixture_loads_as_a_valid_incident() -> None:
    incident = load_incident_by_id("INC-001")
    assert isinstance(incident, Incident)
    assert incident.incident_id == "inc_INC-001"
    assert incident.severity == Severity.HIGH
    assert "analytics.revenue_daily" in incident.affected_assets


def test_inc001_fixture_file_exists_at_the_documented_path() -> None:
    path = DEFAULT_INCIDENT_DIR / "INC-001.json"
    assert path.exists()
    incident = load_incident(path)
    assert incident.incident_id == "inc_INC-001"


def test_incident_detected_at_is_timezone_aware() -> None:
    incident = load_incident_by_id("INC-001")
    assert incident.detected_at.tzinfo is not None
