"""SQL security policy for `query_warehouse` - defense in depth, not one check.

Maturity: hardened.

**The model is never trusted to decide whether SQL is safe.** This module is
the one place that decision is made, and it does not rely on any single
mechanism:

1. **Length bound** - rejected before the SQL is even parsed.
2. **DuckDB's own parser** (`extract_statements`) - the statement must parse,
   there must be exactly one of them, and it must classify as
   `StatementType.SELECT`. This structurally rejects INSERT, UPDATE, DELETE,
   MERGE, DROP, CREATE, ALTER, TRUNCATE, COPY, ATTACH, DETACH, LOAD, INSTALL,
   EXPORT, IMPORT, SET, CALL, VACUUM, EXPLAIN, TRANSACTION and PREPARE - every
   one of them parses to a `StatementType` other than `SELECT`.
3. **A documented gap in (2), closed explicitly.** `PRAGMA database_list`,
   `SHOW TABLES`, `DESCRIBE t` and `SUMMARIZE t` all parse as
   `StatementType.SELECT` - confirmed empirically before this module was
   written, not assumed. A pure statement-type allowlist would let them
   through. This module additionally requires that the *original request
   text's* leading keyword - found by skipping leading whitespace, comments,
   and wrapping parentheses, never by scanning the whole text - be literally
   `SELECT` or `WITH`. This must be checked against the text the caller wrote,
   not `Statement.query`: DuckDB rewrites `PRAGMA database_list` into
   `Statement.query == "SELECT * FROM pragma_database_list;"` before this
   module ever sees it, which would defeat a check performed on the parsed
   form - confirmed empirically, not assumed, after an earlier version of this
   check used `Statement.query` and let a PRAGMA through. Because the check
   only ever looks at a bounded prefix of the caller's text, it cannot be
   confused by a forbidden word appearing later in the query, including inside
   a string literal (`SELECT 'DROP TABLE x' AS note` is unaffected). Separately
   confirmed: DuckDB's grammar has no data-modifying CTE ("a CTE needs a
   SELECT") and no `SELECT ... INTO`, so there is no statement that begins
   with `SELECT` or `WITH` and still performs a write - the leading-keyword
   check is closing a classification gap, not standing in for the structural
   one.
4. **The database connection itself** (`causiq.tools.warehouse`) is opened
   read-only with external access disabled. That is independent, engine-level
   enforcement - not a delegation of this module's job to the connection, and
   not a substitute for it either. Each layer would independently stop most of
   what the other misses; layered, they are how the tool avoids depending on
   any one mechanism being perfect. See ADR-0007 for the empirical basis of
   every claim above and the connection-level configuration.

What this module deliberately does *not* do: it does not attempt to write a
general-purpose SQL parser, and it does not blacklist keywords by scanning the
full query text - the task's own probe showed that "PRAGMA" appearing anywhere
in the text is not a safe signal (it can appear harmlessly inside a string
literal), so every check here is either structural (statement count and type,
from DuckDB's real parser) or positional (the leading keyword only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from causiq.errors import ToolExecutionError

if TYPE_CHECKING:
    import duckdb

#: Generous for a real investigative query, small enough to bound parse cost
#: and keep a rejected request's error message legible.
MAX_SQL_LENGTH: Final = 10_000

#: The only two leading keywords a permitted query may begin with.
_ALLOWED_LEADING_KEYWORDS: Final = frozenset({"SELECT", "WITH"})

#: One unit of "things that may precede the real first keyword": whitespace,
#: a line comment, a block comment, or one wrapping parenthesis. Matched
#: repeatedly (never recursively) by `_leading_keyword`.
_LEADING_TRIVIA: Final = re.compile(
    r"\A(?:\s+|--[^\n]*|/\*.*?\*/|\()",
    re.DOTALL,
)

#: Bound on how many units of trivia are skipped before giving up. A real
#: query never nests this deep; this exists so a pathological input fails
#: fast rather than looping proportionally to a crafted string's length.
_MAX_TRIVIA_UNITS: Final = 64

_LEADING_TOKEN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _leading_keyword(text: str) -> str:
    """The statement's first real token, upper-cased, or "" if none is found.

    Looks only at a bounded prefix of `text` - never the whole string - which
    is what makes it safe against a forbidden word appearing later in the
    query, including inside a string literal.
    """
    remaining = text
    for _ in range(_MAX_TRIVIA_UNITS):
        match = _LEADING_TRIVIA.match(remaining)
        if not match:
            break
        remaining = remaining[match.end() :]
    token = _LEADING_TOKEN.match(remaining)
    return token.group(0).upper() if token else ""


@dataclass(frozen=True)
class ValidatedQuery:
    """One statement that has passed every check in this module.

    `text` is `Statement.query` verbatim - DuckDB's own re-serialization of
    exactly the one statement that was validated - never text this module
    edited or reassembled. What gets executed is provably the same bytes that
    were checked.
    """

    text: str
    statement_type: str


def _reject(message: str, sql: str, **extra: object) -> ToolExecutionError:
    excerpt = sql if len(sql) <= 200 else f"{sql[:200]}…"
    return ToolExecutionError(message, sql_excerpt=excerpt, sql_length=len(sql), **extra)


def validate_query(
    sql: str,
    parser: duckdb.DuckDBPyConnection,
    *,
    max_length: int = MAX_SQL_LENGTH,
) -> ValidatedQuery:
    """Validate `sql` and return the single statement that may be executed.

    Raises `causiq.errors.ToolExecutionError` on any violation - the same
    exception a tool failure would raise, which the executor already turns
    into a FAILED result with no evidence recorded (invariant I1).

    `parser` is used only for `extract_statements()` - pure parsing, never
    `.execute()`. It does not need to be the read-only warehouse connection;
    any DuckDB connection parses SQL identically. Passing the real connection
    is fine and is what `causiq.tools.warehouse` does, since parsing a
    statement performs no I/O regardless of the connection's configuration.
    """
    if len(sql) > max_length:
        msg = f"SQL exceeds the maximum length of {max_length} characters"
        raise _reject(msg, sql, limit=max_length)

    try:
        statements = parser.extract_statements(sql)
    except Exception as exc:
        msg = "SQL failed to parse"
        raise _reject(msg, sql, parser_error=f"{type(exc).__name__}: {exc}") from exc

    if len(statements) != 1:
        msg = f"exactly one SQL statement is required (found {len(statements)})"
        raise _reject(msg, sql, statement_count=len(statements))

    statement = statements[0]
    statement_type = str(statement.type)

    if statement_type != "StatementType.SELECT":
        msg = f"only SELECT/WITH queries are permitted (parsed as {statement_type})"
        raise _reject(msg, sql, statement_type=statement_type)

    # Checked against the CALLER'S text, not `statement.query`: DuckDB rewrites
    # some statements (PRAGMA in particular) into literal SELECT syntax before
    # `.query` is populated, which would defeat this check entirely for the one
    # case it exists to catch. See the module docstring.
    keyword = _leading_keyword(sql)
    if keyword not in _ALLOWED_LEADING_KEYWORDS:
        msg = (
            "query must begin with SELECT or WITH - some statements "
            "(PRAGMA, SHOW, DESCRIBE, SUMMARIZE) parse as SELECT but are not permitted"
        )
        raise _reject(msg, sql, leading_keyword=keyword or "<none>")

    return ValidatedQuery(text=statement.query, statement_type=statement_type)
