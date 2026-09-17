# ADR-0007 — Defense in depth for read-only SQL execution

**Status:** Accepted
**Date:** 2026-09-09
**Requirements served:** 8 (data-quality investigation), 20 (tool permission controls),
AC-11 (SQL guards)

## Context

`query_warehouse` (P0.5) is the first tool that executes model-supplied text against a
real system. The model is never trusted to decide whether SQL is safe (Engineering
Contract 7.3), so every claim in this ADR was verified empirically against DuckDB
1.5.5 before being relied on - none of it is assumed from general SQL knowledge or
from another engine's behaviour.

## Decision

Three independent layers, none asked to be sufficient alone:

### Layer 1 - statement-shape validation (`causiq.tools.sql_policy`)

1. **Length bound**, checked before parsing.
2. **DuckDB's own parser** (`extract_statements`): the text must parse, must yield
   **exactly one** statement, and that statement must classify as
   `StatementType.SELECT`. This alone structurally rejects INSERT, UPDATE, DELETE,
   MERGE, DROP, CREATE, ALTER, TRUNCATE, COPY, ATTACH, DETACH, LOAD, INSTALL, EXPORT,
   SET, CALL, VACUUM, ANALYZE, TRANSACTION, EXPLAIN and EXECUTE - confirmed by
   direct probe, one statement type at a time, not assumed from a keyword list.
3. **A documented gap in (2), closed explicitly.** `PRAGMA database_list`,
   `PRAGMA table_info(...)`, `SHOW TABLES`, `DESCRIBE t` and `SUMMARIZE t` all
   classify as `StatementType.SELECT`. The additional check: the *caller's original
   text*, after skipping leading whitespace/comments/parentheses (a bounded,
   positional scan - never a scan of the whole query), must begin with `SELECT` or
   `WITH`.

### Layer 2 - the database connection itself

A fresh connection per query: file-based (`:memory:` cannot be opened read-only -
DuckDB raises `CatalogException` if attempted, which is why the evidence substrate
is a file), `read_only=True`, `config={"enable_external_access": False,
"lock_configuration": True}`.

### Layer 3 - bounded execution

Row cap via `fetchmany(max_rows + 1)` (measurably avoids materialising a large
result - see below, not merely truncating one after the fact), a result-byte cap,
and a timeout enforced by `threading.Timer(..., con.interrupt)` - genuine
cancellation, not abandonment (see below).

## Alternatives considered

**A naive prefix check** (`sql.lower().startswith("select")`). Rejected outright -
explicitly ruled out by the task this ADR responds to, and demonstrably wrong: it
does not survive a leading comment, does not reject a second statement appended
after a semicolon, and does not catch `PRAGMA`.

**A pure `StatementType` allowlist**, with no positional check. This was the first
design considered and was empirically shown to be insufficient during this
project - see "Bugs found and fixed during implementation" below. Rejected once the
PRAGMA gap was found.

**A full custom SQL parser.** Rejected: DuckDB already has a correct one, exposed
via `extract_statements`. Reimplementing it would be strictly worse and untested
against DuckDB's actual grammar.

**A textual keyword blacklist scanning the whole query** (reject if `"DROP"`,
`"PRAGMA"`, etc. appear anywhere). Rejected: the task's own brief identified the
failure mode, and it was reproduced - `SELECT 'DROP TABLE x' AS note` is harmless
data that a whole-text blacklist would incorrectly reject.

## Empirical findings that shaped the design

Every claim below was produced by running the exact statement against DuckDB
1.5.5, not recalled from documentation:

- `extract_statements` correctly tokenizes comments and string literals before
  classifying statements - a forbidden word inside a string literal or a comment
  does not affect statement count or type, and a `;` inside a `--` comment is not
  treated as a statement separator.
- A `read_only=True` file connection blocks CREATE/INSERT/UPDATE/DELETE/DROP/
  ALTER/TRUNCATE/MERGE at the engine with `InvalidInputException` - genuine
  engine-level enforcement, verified by attempting each one.
- `enable_external_access=False` blocks `read_csv*`, `read_parquet*`, `glob`,
  `ATTACH`, `DETACH`, `LOAD`, `INSTALL`, `COPY`, `EXPORT`/`IMPORT` with
  `PermissionException`, and - unprompted by any extra configuration - **refuses to
  be re-enabled while the database is running** (`Cannot enable external access
  while database is running`). `lock_configuration=True` additionally blocks any
  `SET` from changing configuration at all.
- **DuckDB's grammar has no data-modifying CTE** (`WITH x AS (DELETE ... ) SELECT
  ...` fails to parse: `"A CTE needs a SELECT"`) **and no `SELECT ... INTO`**
  (`"SELECT INTO not supported!"`). This was checked specifically because a
  leading-keyword allowlist of `SELECT`/`WITH` would be unsound if either existed -
  it does not, so the allowlist is closing a *classification* gap, not standing in
  for the *structural* one.
- `con.interrupt()`, called from a separate thread while a query runs, raises
  `duckdb.InterruptException` in the executing thread within the timer's interval -
  timed directly: a query that would otherwise take tens of seconds (a large cross
  join) was aborted in ~0.3s, and the connection was immediately reusable
  afterward. This is what makes P0.5's timeout a genuine cancellation, not the
  abandon-only mechanism documented as a known limitation in `causiq.tools.executor`
  (P0.4).
- `fetchmany(n)` measurably avoids materialising more than `n` rows: `fetchmany(3)`
  against a 30-million-row query returned in 0.003s; `fetchall()` against the same
  query took 18.5s. The row cap is a real bound on engine work, not a truncation
  applied after full computation.

## Bugs found and fixed during implementation

Recorded here because they are exactly the kind of thing a review should be able
to ask about, and because they justify why this ADR insists on empirical
verification rather than confidence:

1. **The leading-keyword check was first implemented against `Statement.query`
   (the parser's own re-serialization), not the caller's original text.**
   `PRAGMA database_list` was found to still succeed, because DuckDB rewrites it
   internally to `Statement.query == "SELECT * FROM pragma_database_list;"` before
   the check ever saw it. Fixed by checking the caller's `sql` argument instead.
   `SHOW`/`DESCRIBE`/`SUMMARIZE` were unaffected by this bug (DuckDB does not
   rewrite their `.query`), but the fix is what makes PRAGMA's rejection
   correct rather than accidental. A regression test
   (`test_pragma_rewritten_query_would_have_passed_a_naive_check`) pins this.
2. **The row-count truncation flag was not threaded into the JSON content's own
   `"truncated"` field** on the first serialization attempt - only byte-driven
   shrinking set it, so a result capped purely by row count could report
   `"truncated": false` in its own body while `ToolOutput.truncated` (read by the
   executor) correctly said `true`. Fixed by passing the row-truncation state into
   the byte-capping serializer as its starting value.

## Consequences

**Positive.** Every SQL-shaped rejection in the task's required test matrix is
either structural (DuckDB's real parser) or positional (a bounded prefix scan),
never a whole-text keyword scan - so it cannot misfire on data that merely
resembles a forbidden word. The timeout is a tested guarantee, not an assumption:
a slow query is provably aborted in a fraction of a second, not merely abandoned
while it keeps running.

**Limitations, stated precisely rather than overstated:**

- The leading-keyword check is a *supplementary* defense closing one documented
  classification gap (statements DuckDB's own type system calls SELECT but which
  are not literally SELECT/WITH). It is not, by itself, adversarially hardened
  against every conceivable comment-obfuscation technique - but it does not need
  to be: DuckDB's own parser (Layer 1's primary check) and the connection lockdown
  (Layer 2) are what actually prevent a write or an external access, independent
  of whether the leading-keyword check is perfect. The leading-keyword check's
  worst-case failure mode is letting through a read-only, catalog-introspection
  statement class (PRAGMA/SHOW/DESCRIBE/SUMMARIZE) it failed to recognise - never
  a write, and never file/network access, because those remain blocked at Layer 2
  regardless.
- **`VACUUM` and `ANALYZE` are, surprisingly, permitted by DuckDB's own
  `read_only=True` engine enforcement** (they ran successfully against a read-only
  connection in direct testing). They are still rejected in Causiq because Layer 1
  independently requires `StatementType.SELECT`, and both parse as
  `StatementType.VACUUM`. This is recorded because it is a case where the
  layers are *not* redundant with each other - Layer 1 is doing real, necessary
  work that Layer 2 alone would not have done.
- Connection-per-query (rather than a single reused connection) trades a small
  per-query connection-open cost for eliminating any possibility of one query's
  timer or interrupted state affecting a later, unrelated query on a shared
  connection. Not measured as a performance concern at the row/time budgets P0.5
  uses; would need revisiting if the tool were ever driven at much higher
  throughput.

## How we test it

`tests/unit/test_sql_policy.py` (55 tests: every required rejection category, the
documented PRAGMA gap with a named regression test, whitespace/comment/case/
quoting edge cases, and the bounded-loop exhaustion path itself);
`tests/unit/test_warehouse_tool.py` (limits, the timed proof of genuine
cancellation via `interrupt()`, and full executor-integration - authorization,
evidence, audit, no-evidence-on-failure, and the structural no-bypass proof that
`Tool.run`'s signature has no ledger/journal parameter);
`tests/integration/test_inc001_investigation.py` (the real tool against the real
seeded incident, deriving the root cause from query results, not a hardcoded
string).

## Review script

*"I didn't design this from what I remembered about SQL security - I ran DuckDB
1.5.5 and watched what it actually did, including watching it surprise me twice:
`PRAGMA database_list` classifies as SELECT but rewrites to literal SELECT syntax
before my first check saw it, and `VACUUM` is permitted by the read-only engine
itself even though my application-level check rejects it anyway. Both surprises
are in this document with the exact probe that found them, and both are why there
are three independent layers instead of one clever one."*
