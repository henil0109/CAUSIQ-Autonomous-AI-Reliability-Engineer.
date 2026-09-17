"""Build the INC-001 evidence substrate for local use.

A thin CLI wrapper - the actual logic lives in
`causiq.evidence_substrate.build_warehouse_db` so it is importable and covered
by the test suite (`tests/unit/test_evidence_substrate.py`) independently of
this script.

    uv run python scripts/seed_warehouse.py
    uv run python scripts/seed_warehouse.py --force
    uv run python scripts/seed_warehouse.py --out var/warehouse/custom.duckdb
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from causiq.evidence_substrate import (
    DEFAULT_SEED_SQL_PATH,
    DEFAULT_WAREHOUSE_PATH,
    build_warehouse_db,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_WAREHOUSE_PATH,
        help=f"output DuckDB file (default: {DEFAULT_WAREHOUSE_PATH})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="delete an existing file at --out before building",
    )
    args = parser.parse_args()

    if args.force and args.out.exists():
        args.out.unlink()

    build_warehouse_db(args.out, seed_sql_path=DEFAULT_SEED_SQL_PATH)
    sys.stdout.write(f"built {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
