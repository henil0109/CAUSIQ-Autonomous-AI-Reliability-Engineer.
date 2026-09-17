"""Build and load the deterministic DuckDB evidence substrate (ADR-0003).

Maturity: hardened.

This module owns the *write* side of the evidence substrate: turning
`fixtures/warehouse/seed.sql` into a real DuckDB file, and loading an incident
fixture into the domain model. It is deliberately the only code in the project
that opens the warehouse file read-write - `causiq.tools.warehouse` (the
investigation tool) only ever opens it read-only, and this separation is what
keeps a security review of the tool short: nothing in that module can write.

Why the fixture is executed as a plain SQL script rather than built row-by-row
in Python: the seed *is* a small, real warehouse migration, and reading it as
SQL is what a reviewer, and the eventual dbt/Airflow artifacts in Phase 1,
would expect it to look like. Determinism (invariant I6) comes from the SQL
itself containing no `RANDOM()`, `NOW()`, or `UUID()` - every value is a pure
function of two loop counters and literal constants - not from anything this
module does.
"""

from __future__ import annotations

import json
from pathlib import Path

from causiq.domain import Incident

#: Repository root, derived from this file's location rather than the current
#: working directory - so callers get the same fixtures regardless of where a
#: script or test runner was invoked from.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_SEED_SQL_PATH = REPO_ROOT / "fixtures" / "warehouse" / "seed.sql"
DEFAULT_INCIDENT_DIR = REPO_ROOT / "fixtures" / "incidents"

#: Where `scripts/seed_warehouse.py` writes by default. Under `var/`, which is
#: gitignored (Engineering Contract: runtime output is not source).
DEFAULT_WAREHOUSE_PATH = REPO_ROOT / "var" / "warehouse" / "inc001.duckdb"


def build_warehouse_db(db_path: Path, *, seed_sql_path: Path = DEFAULT_SEED_SQL_PATH) -> None:
    """Create `db_path` fresh and populate it from `seed_sql_path`.

    Read-write by necessity - this is the one function in the project allowed
    to write to the warehouse file. `db_path` must not already exist; callers
    that want to rebuild call `db_path.unlink(missing_ok=True)` first, so an
    accidental rebuild is always an explicit choice, not a silent overwrite.

    Import is local to keep `duckdb` out of every module that merely imports
    this file's constants (e.g. anything that only wants `DEFAULT_*` paths).
    """
    import duckdb

    if db_path.exists():
        msg = f"refusing to overwrite an existing warehouse file: {db_path}"
        raise FileExistsError(msg)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    script = seed_sql_path.read_text(encoding="utf-8")
    con = duckdb.connect(str(db_path))
    try:
        con.execute(script)
    finally:
        con.close()


def load_incident(path: Path) -> Incident:
    """Load and validate one incident fixture."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return Incident.model_validate(data)


def load_incident_by_id(incident_id: str, *, directory: Path = DEFAULT_INCIDENT_DIR) -> Incident:
    """Load the fixture named `<incident_id>.json` from `directory`.

    `incident_id` is the bare id used in filenames (e.g. `INC-001`), not the
    prefixed `causiq.ids.IncidentId` value stored inside the fixture.
    """
    return load_incident(directory / f"{incident_id}.json")
