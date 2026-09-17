"""`query_warehouse` - the first real investigation tool (P0.5).

Maturity: hardened.

Trust boundary, stated once here because every other document about this tool
refers back to it (ADR-0007 has the full empirical basis):

    model
      v
    Tool Registry        (causiq.tools.registry - is this even a known tool?)
      v
    Tool Executor        (causiq.tools.executor  - the security boundary)
      v
    Authorization         (causiq.authz - identity + permission + approval)
      v
    query_warehouse        <- this module
      v
    DuckDB (read-only, external access disabled)

**The model is never trusted to decide whether SQL is safe.** Everything above
this module enforces that a request even reaches here; everything inside
`QueryWarehouseTool.run` enforces that what reaches here cannot do anything but
read. Three independent layers, none of which is asked to be sufficient alone:

1. `causiq.tools.sql_policy` - statement-shape validation before execution.
2. A fresh, read-only, external-access-disabled DuckDB connection per query -
   engine-level enforcement, empirically verified (ADR-0007), not assumed.
3. Bounded execution: row cap via `fetchmany` (which measurably avoids
   materialising a large result, not merely truncating one after the fact),
   a result-byte cap, and a genuine cancellation-based timeout.

This module never touches the evidence ledger or the audit journal. `run()`
returns a `ToolOutput`; the executor is the only code that turns a successful
one into `Evidence` (invariant I1) or a failed one into an audited, evidence-
free rejection. Nothing here can bypass that, structurally: `run()` has no
parameter through which a ledger or journal could even be reached.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import duckdb
from pydantic import Field

from causiq.authz import Permission
from causiq.domain.enums import EvidenceSource, RiskLevel
from causiq.errors import ToolTimeoutError
from causiq.tools.base import ToolInput, ToolOutput, ToolSpec, canonical_json
from causiq.tools.sql_policy import MAX_SQL_LENGTH, validate_query

#: Defaults, chosen to be generous for a real investigative query while
#: keeping a single result small enough to sit comfortably in a model's
#: context window alongside everything else in a run.
DEFAULT_MAX_ROWS: Final = 500
DEFAULT_MAX_RESULT_BYTES: Final = 256_000
#: The tool's own, DuckDB-`interrupt()`-backed deadline. Deliberately shorter
#: than the executor's outer bound (below), so this fires first under normal
#: conditions and the outer bound is a true last resort.
DEFAULT_QUERY_TIMEOUT_SECONDS: Final = 5.0
#: The executor's outer, abandon-only bound (ToolSpec.timeout_seconds). Kept
#: comfortably above the inner timeout so it is never the mechanism that
#: actually fires in ordinary operation.
DEFAULT_OUTER_TIMEOUT_SECONDS: Final = 10.0

#: Connection configuration is identical for every query and stated once here
#: rather than re-derived per call. `enable_external_access=False` is what
#: blocks read_csv*/read_parquet*/glob/ATTACH/DETACH/LOAD/INSTALL/COPY/EXPORT/
#: IMPORT at the engine (verified empirically, ADR-0007); `lock_configuration`
#: additionally blocks a query from changing any configuration option,
#: including re-enabling external access.
#: Typed to match `duckdb.connect`'s `config` parameter exactly - dict is
#: invariant, so a plain `dict[str, bool]` inference would not satisfy it.
_CONNECTION_CONFIG: Final[dict[str, str | bool | int | float | list[str]]] = {
    "enable_external_access": False,
    "lock_configuration": True,
}


class WarehouseQueryInput(ToolInput):
    """Arguments for `query_warehouse`.

    The length bound is enforced twice: here, at the schema level (so a
    too-long request is rejected before the tool ever runs), and again inside
    `sql_policy.validate_query` (so the tool is correct even if ever invoked
    with a hand-built payload that skipped schema validation). Neither
    enforcement depends on the other.
    """

    sql: str = Field(
        min_length=1,
        max_length=MAX_SQL_LENGTH,
        description=(
            "A single read-only SQL statement: SELECT or WITH ... SELECT. "
            "No other statement type, and no more than one statement, is permitted."
        ),
    )


def _column_metadata(description: Sequence[tuple[object, ...]] | None) -> list[dict[str, str]]:
    """Column name and DuckDB type from a DBAPI cursor `.description`.

    Typed loosely (`tuple[object, ...]`) rather than against DuckDB's exact
    7-tuple stub: this function only ever reads the first two positions and
    stringifies them, so the precise type of the remaining DBAPI columns
    (always `None` for DuckDB) is not this function's concern.
    """
    if not description:
        return []
    return [{"name": str(col[0]), "type": str(col[1])} for col in description]


class QueryWarehouseTool:
    """Read-only SQL access to the seeded DuckDB warehouse.

    One instance is stateless and safe to register once for the life of a
    process - every call opens and closes its own connection (see
    `_open_connection`), so there is no shared, reusable connection whose
    state could leak between queries.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        name: str = "query_warehouse",
        max_rows: int = DEFAULT_MAX_ROWS,
        max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
        query_timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
        outer_timeout_seconds: float = DEFAULT_OUTER_TIMEOUT_SECONDS,
    ) -> None:
        if query_timeout_seconds >= outer_timeout_seconds:
            msg = (
                "query_timeout_seconds must be strictly less than outer_timeout_seconds, "
                "so the tool's own cancellation fires before the executor's abandon-only bound"
            )
            raise ValueError(msg)
        self._db_path = db_path
        self._max_rows = max_rows
        self._max_result_bytes = max_result_bytes
        self._query_timeout_seconds = query_timeout_seconds
        self._spec = ToolSpec(
            name=name,
            description=(
                "Run one read-only SQL query (SELECT or WITH ... SELECT) against the "
                "warehouse and return the result as evidence. State a clear reason: "
                "what you expect this query to show and why."
            ),
            permission=Permission.WAREHOUSE_READ,
            mutating=False,
            risk=RiskLevel.LOW,
            source=EvidenceSource.WAREHOUSE,
            timeout_seconds=outer_timeout_seconds,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def input_model(self) -> type[ToolInput]:
        return WarehouseQueryInput

    def _open_connection(self) -> duckdb.DuckDBPyConnection:
        """A fresh, locked-down connection for exactly one query.

        `:memory:` cannot be opened read-only (DuckDB raises a CatalogException
        if it is attempted), which is precisely why the evidence substrate is a
        file (`causiq.evidence_substrate.build_warehouse_db`) rather than an
        in-memory database - read-only enforcement requires it.
        """
        return duckdb.connect(str(self._db_path), read_only=True, config=_CONNECTION_CONFIG)

    def run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, WarehouseQueryInput)
        con = self._open_connection()
        try:
            validated = validate_query(payload.sql, con)

            timer = threading.Timer(self._query_timeout_seconds, con.interrupt)
            timer.start()
            try:
                result = con.execute(validated.text)
                columns = _column_metadata(result.description)
                rows = result.fetchmany(self._max_rows + 1)
            except duckdb.InterruptException as exc:
                msg = (
                    f"query exceeded its {self._query_timeout_seconds}s timeout "
                    f"and was cancelled by DuckDB's interrupt() - not merely abandoned"
                )
                raise ToolTimeoutError(
                    msg, query_timeout_seconds=self._query_timeout_seconds
                ) from exc
            finally:
                timer.cancel()

            row_truncated = len(rows) > self._max_rows
            if row_truncated:
                rows = rows[: self._max_rows]

            content, truncated = self._serialize_capped(
                validated.text, columns, rows, truncated=row_truncated
            )
            return ToolOutput(
                content=content,
                content_type="application/json",
                truncated=truncated,
            )
        finally:
            con.close()

    def _serialize_capped(
        self,
        sql: str,
        columns: list[dict[str, str]],
        rows: list[tuple[object, ...]],
        *,
        truncated: bool,
    ) -> tuple[str, bool]:
        """Serialize the result, dropping trailing rows until under the byte cap.

        `truncated` starts as whatever the caller already knows (e.g. the row
        cap was hit) so the content's own `"truncated"` field is correct even
        on the very first render, before any byte-driven shrinking happens.

        `canonical_json` (shared with the tool schema emitter) handles
        non-JSON-native cell types - dates, decimals - via its `default=str`
        fallback, so no separate cell-encoding logic is needed here.
        """

        def render(subset: list[tuple[object, ...]]) -> str:
            return canonical_json(
                {
                    "sql": sql,
                    "columns": columns,
                    "rows": [list(row) for row in subset],
                    "row_count": len(subset),
                    "truncated": truncated,
                }
            )

        content = render(rows)
        while len(content.encode("utf-8")) > self._max_result_bytes and rows:
            rows = rows[:-1]
            truncated = True
            content = render(rows)
        return content, truncated
