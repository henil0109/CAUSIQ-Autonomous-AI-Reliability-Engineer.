"""SQL security policy - defense in depth for `query_warehouse`.

Every rejection case here is paired, where practical, with a "looks similar but
must be ALLOWED" case, per the task's own warning: a blacklist that fires on a
forbidden word anywhere in the text is wrong even when it denies something,
because it will eventually deny something harmless too. The point of this
module (and this test file) is that every rejection is either structural
(DuckDB's own parser said so) or positional (the leading keyword, and nothing
after it).
"""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest

from causiq.errors import ToolExecutionError
from causiq.tools.sql_policy import MAX_SQL_LENGTH, validate_query

pytestmark = pytest.mark.unit


@pytest.fixture
def parser() -> Iterator[duckdb.DuckDBPyConnection]:
    """A throwaway connection used only for `extract_statements` (pure parsing,
    no execution) - any DuckDB connection parses SQL identically, so this does
    not need to be the locked-down warehouse connection."""
    con = duckdb.connect(":memory:")
    yield con
    con.close()


# --------------------------------------------------------------------------- #
# A. Happy path
# --------------------------------------------------------------------------- #
def test_simple_select_is_allowed(parser: duckdb.DuckDBPyConnection) -> None:
    result = validate_query("SELECT 1", parser)
    assert result.statement_type == "StatementType.SELECT"
    assert result.text == "SELECT 1"


def test_with_select_is_allowed(parser: duckdb.DuckDBPyConnection) -> None:
    result = validate_query("WITH t AS (SELECT 1 AS x) SELECT x FROM t", parser)
    assert result.statement_type == "StatementType.SELECT"


def test_nested_subquery_is_allowed(parser: duckdb.DuckDBPyConnection) -> None:
    validate_query("SELECT * FROM (SELECT 1 AS x) AS sub", parser)


def test_parenthesized_top_level_select_is_allowed(parser: duckdb.DuckDBPyConnection) -> None:
    """DuckDB accepts `(SELECT ...)` as a bare top-level statement."""
    validate_query("(SELECT 1)", parser)


# --------------------------------------------------------------------------- #
# B. Forbidden statement types - each structurally rejected by StatementType
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("label", "sql"),
    [
        ("INSERT", "INSERT INTO t VALUES (1)"),
        ("UPDATE", "UPDATE t SET x = 1"),
        ("DELETE", "DELETE FROM t"),
        ("MERGE", "MERGE INTO t USING t AS s ON t.x = s.x WHEN MATCHED THEN DELETE"),
        ("DROP", "DROP TABLE t"),
        ("CREATE", "CREATE TABLE t2(x INT)"),
        ("ALTER", "ALTER TABLE t ADD COLUMN y INT"),
        ("TRUNCATE", "TRUNCATE t"),  # DuckDB's TRUNCATE parses as StatementType.DELETE
        ("COPY", "COPY t TO 'out.csv'"),
        ("ATTACH", "ATTACH 'other.db'"),
        ("DETACH", "DETACH other"),
        ("LOAD", "LOAD 'httpfs'"),
        ("INSTALL", "INSTALL 'httpfs'"),
        ("EXPORT", "EXPORT DATABASE 'outdir'"),
        ("EXECUTE", "EXECUTE my_prepared_statement"),
        ("SET (state-changing PRAGMA)", "PRAGMA memory_limit='1GB'"),
        ("SET", "SET memory_limit='500MB'"),
        ("CALL", "CALL pragma_table_info('t')"),
        ("VACUUM", "VACUUM"),
        ("ANALYZE", "ANALYZE t"),
        ("TRANSACTION", "BEGIN TRANSACTION"),
        ("EXPLAIN", "EXPLAIN SELECT 1"),
    ],
)
def test_forbidden_statement_type_is_rejected(
    label: str, sql: str, parser: duckdb.DuckDBPyConnection
) -> None:
    with pytest.raises(ToolExecutionError) as caught:
        validate_query(sql, parser)
    assert caught.value.recoverable is True, label


def test_import_is_rejected(parser: duckdb.DuckDBPyConnection) -> None:
    """IMPORT fails to parse cleanly without a real export directory present;
    either a parse failure or a statement-type rejection is an acceptable
    outcome - both reject the request and record no evidence."""
    with pytest.raises(ToolExecutionError):
        validate_query("IMPORT DATABASE 'outdir'", parser)


# --------------------------------------------------------------------------- #
# C. The documented parser gap: statements that classify as SELECT but are not
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("label", "sql"),
    [
        ("PRAGMA (rewritten to SELECT internally)", "PRAGMA database_list"),
        ("PRAGMA with arguments", "PRAGMA table_info('t')"),
        ("SHOW TABLES", "SHOW TABLES"),
        ("DESCRIBE", "DESCRIBE t"),
        ("SUMMARIZE", "SUMMARIZE t"),
    ],
)
def test_select_misclassified_shorthand_is_still_rejected(
    label: str, sql: str, parser: duckdb.DuckDBPyConnection
) -> None:
    """Confirmed empirically: DuckDB classifies all of these as
    `StatementType.SELECT`. A pure statement-type allowlist would accept them;
    the leading-keyword check must catch them instead."""
    # Precondition, stated as an assertion rather than a comment, so a DuckDB
    # upgrade that changes this classification fails loudly here rather than
    # silently weakening what this test proves.
    assert str(parser.extract_statements(sql)[0].type) == "StatementType.SELECT", label
    with pytest.raises(ToolExecutionError, match="must begin with SELECT or WITH"):
        validate_query(sql, parser)


def test_pragma_rewritten_query_would_have_passed_a_naive_check(
    parser: duckdb.DuckDBPyConnection,
) -> None:
    """Regression guard for the exact bug this module was built to avoid.

    `Statement.query` for a PRAGMA is DuckDB's *rewritten* form - literal
    `SELECT` syntax - not the caller's original text. A leading-keyword check
    performed on `.query` instead of the caller's `sql` would have let this
    through; this test fails if that regression is reintroduced.
    """
    statement = parser.extract_statements("PRAGMA database_list")[0]
    assert statement.query.strip().upper().startswith("SELECT")
    with pytest.raises(ToolExecutionError):
        validate_query("PRAGMA database_list", parser)


# --------------------------------------------------------------------------- #
# C. Parsing edge cases - whitespace, comments, case, quoting
# --------------------------------------------------------------------------- #
def test_leading_and_trailing_whitespace_is_tolerated(parser: duckdb.DuckDBPyConnection) -> None:
    validate_query("   \n\t SELECT 1  \n", parser)


def test_mixed_case_keyword_is_allowed(parser: duckdb.DuckDBPyConnection) -> None:
    validate_query("SeLeCt 1", parser)
    validate_query("with t as (select 1) select * from t", parser)


def test_leading_line_comment_before_select_is_allowed(
    parser: duckdb.DuckDBPyConnection,
) -> None:
    validate_query("-- explain the drop\nSELECT 1", parser)


def test_leading_block_comment_before_select_is_allowed(
    parser: duckdb.DuckDBPyConnection,
) -> None:
    validate_query("/* note */ SELECT 1", parser)


def test_multiple_leading_comments_are_all_skipped(parser: duckdb.DuckDBPyConnection) -> None:
    validate_query("-- one\n/* two */\n-- three\nSELECT 1", parser)


def test_trailing_semicolon_is_tolerated(parser: duckdb.DuckDBPyConnection) -> None:
    validate_query("SELECT 1;", parser)
    validate_query("SELECT 1 ;  ", parser)


def test_trailing_comment_containing_a_semicolon_is_not_a_second_statement(
    parser: duckdb.DuckDBPyConnection,
) -> None:
    """A `;` inside a comment must not be mistaken for a statement separator."""
    validate_query("SELECT 1 -- trailing comment; DROP TABLE x", parser)


def test_forbidden_word_inside_a_string_literal_is_not_flagged(
    parser: duckdb.DuckDBPyConnection,
) -> None:
    """The core promise of a positional (not textual) check: a forbidden
    keyword appearing as *data*, anywhere but the leading position, must not
    cause a rejection."""
    validate_query("SELECT 'DROP TABLE x' AS note", parser)
    validate_query("SELECT 'PRAGMA fake, not a statement' AS note", parser)
    validate_query("SELECT 1 /* INSERT INTO hidden */", parser)


def test_column_alias_resembling_a_forbidden_word_is_not_flagged(
    parser: duckdb.DuckDBPyConnection,
) -> None:
    validate_query("SELECT 1 AS pragma_like_alias", parser)


def test_quoted_identifier_resembling_a_forbidden_word_is_not_flagged(
    parser: duckdb.DuckDBPyConnection,
) -> None:
    validate_query('SELECT 1 AS "drop"', parser)


# --------------------------------------------------------------------------- #
# D. Multiple statements
# --------------------------------------------------------------------------- #
def test_two_statements_are_rejected(parser: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(ToolExecutionError, match="exactly one SQL statement"):
        validate_query("SELECT 1; SELECT 2", parser)


def test_select_followed_by_a_write_is_rejected_as_multiple_statements(
    parser: duckdb.DuckDBPyConnection,
) -> None:
    """A SELECT smuggling a second, dangerous statement after it must not be
    allowed just because the first statement is harmless."""
    with pytest.raises(ToolExecutionError, match="exactly one SQL statement"):
        validate_query("SELECT 1; DROP TABLE t", parser)


def test_empty_input_is_rejected(parser: duckdb.DuckDBPyConnection) -> None:
    """Parses to zero statements - "exactly one" correctly rejects this too."""
    with pytest.raises(ToolExecutionError, match="exactly one SQL statement"):
        validate_query("", parser)


def test_whitespace_only_input_is_rejected(parser: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(ToolExecutionError, match="exactly one SQL statement"):
        validate_query("   \n\t  ", parser)


def test_comment_only_input_is_rejected(parser: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(ToolExecutionError, match="exactly one SQL statement"):
        validate_query("-- just a comment", parser)


# --------------------------------------------------------------------------- #
# Length limit
# --------------------------------------------------------------------------- #
def test_sql_exceeding_max_length_is_rejected(parser: duckdb.DuckDBPyConnection) -> None:
    huge = "SELECT " + "1" * MAX_SQL_LENGTH
    with pytest.raises(ToolExecutionError, match="maximum length") as caught:
        validate_query(huge, parser)
    assert caught.value.context["limit"] == MAX_SQL_LENGTH


def test_sql_at_exactly_the_limit_is_not_rejected_for_length(
    parser: duckdb.DuckDBPyConnection,
) -> None:
    padding = "SELECT " + "1" * (MAX_SQL_LENGTH - len("SELECT  AS x"))
    sql = padding + " AS x"
    assert len(sql) <= MAX_SQL_LENGTH
    validate_query(sql, parser)


def test_custom_max_length_is_honoured(parser: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(ToolExecutionError, match="maximum length"):
        validate_query("SELECT 1", parser, max_length=4)


# --------------------------------------------------------------------------- #
# Malformed SQL
# --------------------------------------------------------------------------- #
def test_syntactically_invalid_sql_is_rejected(parser: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(ToolExecutionError, match="failed to parse"):
        validate_query("SELECT FROM WHERE", parser)


def test_error_context_includes_a_bounded_excerpt(parser: duckdb.DuckDBPyConnection) -> None:
    """The excerpt aids debugging without dumping unbounded text into logs."""
    with pytest.raises(ToolExecutionError) as caught:
        validate_query("INSERT INTO t VALUES (1)", parser)
    assert "sql_excerpt" in caught.value.context
    assert caught.value.context["sql_length"] == len("INSERT INTO t VALUES (1)")


def test_very_long_forbidden_sql_excerpt_is_truncated(parser: duckdb.DuckDBPyConnection) -> None:
    sql = "SELECT " + "x" * 500 + " FROM t; SELECT 2"
    with pytest.raises(ToolExecutionError) as caught:
        validate_query(sql, parser)
    excerpt = caught.value.context["sql_excerpt"]
    assert isinstance(excerpt, str)
    assert len(excerpt) <= 201  # 200 chars + the ellipsis marker


# --------------------------------------------------------------------------- #
# The leading-trivia skip is bounded - proven, not merely relied upon
# --------------------------------------------------------------------------- #
def test_pathologically_deep_leading_parentheses_are_safely_rejected(
    parser: duckdb.DuckDBPyConnection,
) -> None:
    """`_leading_keyword`'s trivia-skip loop is capped at 64 units so a
    crafted input cannot make it loop proportionally to the input's length.
    Beyond the cap it stops skipping and correctly finds no real keyword at
    the (still-parenthesized) remaining position, so this is rejected rather
    than hanging or mis-classifying - exercising the loop's exhaustion path,
    not just its early-break path every other test above takes."""
    # DuckDB's own parser also bounds nesting, so this must fail to parse
    # cleanly as a single valid SELECT long before it could matter either way
    # - the assertion is simply that validation terminates and rejects it.
    deeply_nested = "(" * 100 + "SELECT 1" + ")" * 100
    with pytest.raises(ToolExecutionError):
        validate_query(deeply_nested, parser)
