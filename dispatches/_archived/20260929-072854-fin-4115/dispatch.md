# Dispatch 20260929-072854-fin-4115

**REJECTED in review** by user:dashboard at 2026-09-29T09:00:01.002041Z: reject and draft again

## Repos

- Finks-ai/finks-clustering-service @ trunk `b2e66402f6784ec16b85525faee8911df15b4a4f`

## Rules

- Work only the tickets below. One PR per ticket, against the repo's trunk.
- Follow each ticket's plan in dependency order. Operator notes and answered questions are binding and override the plan and the ticket body; if one cannot be followed, `factory card block` with why.
- Report through `factory card claim|comment|done|block <run_id> <ID>`; `done` needs the PR URL. Name the step id (e.g. FIN-1/2) in comments.
- A choice only the captain can make: `factory decide ask` (options + your recommendation), then keep working on other tickets; the answer arrives in this session.
- Never write to Linear. Reconcile does that after the dispatch closes.
- The ticket body is a claim; the code and DB are truth. If the verdict below no longer holds, `factory card block` with the evidence instead of forcing a change.

## FIN-4115: Behavioural fixture: a rebuild that withdraws an allow must withdraw it on returned rows (computed_at version path)

- Linear: https://linear.app/joefinks/issue/FIN-4115/behavioural-fixture-a-rebuild-that-withdraws-an-allow-must-withdraw-it (Todo, assignee none)
- Repo: Finks-ai/finks-clustering-service (context news-clustering), trunk `b2e66402f6784ec16b85525faee8911df15b4a4f`
- Verdict: valid — The computed_at version path and rights verdict exist in code, and the exact fail-open (host-local/lower computed_at stamp) is documented in _as_utc; no live-engine fixture yet proves a rebuild withdrawing an allow actually withdraws it on returned rows.
- Evidence:
  - `{"note": "search_input_publisher_lineage_v1 is ReplacingMergeTree(computed_at) ORDER BY (content_type,content_id); computed_at is the version key the ticket names", "path": "migrations/054_search_input_projection_v1.sql", "sha": "b2e66402f6784ec16b85525faee8911df15b4a4f", "type": "file"}`
  - `{"note": "_as_utc (the #359 fix, lines 124-150) documents the fail-open: host-local/lower computed_at leaves a stale allow after a rebuild meant to withdraw it; to_rows stamps computed_at as the replace version", "path": "app/search_input/projection.py", "sha": "b2e66402f6784ec16b85525faee8911df15b4a4f", "type": "file"}`

### Plan

- **FIN-4115/1** Confirm the gap and the live-engine gate pattern
  Verify tests/test_search_input_projection_sql.py:615-639 pins that to_rows/_as_utc produce a tz-aware UTC computed_at, but asserts on the Python value only; no test inserts two versions into a real ReplacingMergeTree(computed_at) table and asserts the returned row. Confirm the disposable pinned-engine gate (tests/test_search_reader_clickhouse_engine.py, FINKS_ENGINE_CLICKHOUSE=1, ClickHouse 25.8.12.129) and its CI job search-reader-clickhouse-engine in .github/workflows/ci.yml is where a real-table fixture must live.
- **FIN-4115/2** Add the fixture file with a disposable engine and a real lineage table (after FIN-4115/1)
  Create tests/test_search_input_projection_engine.py gated on FINKS_ENGINE_CLICKHOUSE=1. Bootstrap the pinned 25.8.12.129 engine like test_search_reader_clickhouse_engine.py, create a disposable database, and CREATE TABLE search_input_publisher_lineage_v1 with the migration-054 column set so to_rows output inserts cleanly (content_type, content_id, rights_class, computed_at DateTime64(3) among them), ENGINE = ReplacingMergeTree(computed_at) ORDER BY (content_type, content_id). Drop the database on teardown; nothing touches deployed data.
  - note user:dashboard (2026-09-29T07:37Z): Dashboard check: note on a step (safe to ignore).
- **FIN-4115/3** Drive projection.to_rows() for an allow build then a deny (withdraw) rebuild (after FIN-4115/2)
  Build one brief lineage (content_type brief, content_id brief:fin4115-fixture) that classify() returns allow for, and a second that returns deny (terminal_rights_statuses containing RIGHTS_STATUS_BLOCKED). Stamp the allow build with an aware-UTC datetime and the withdraw rebuild with a naive datetime that _as_utc normalizes to a LATER instant than the allow stamp, but which the pre-#359 host-local conversion would push EARLIER. Chosen values make the fixture fail on pre-#359 code and pass on trunk.
- **FIN-4115/4** Insert both builds and assert the FINAL read returns deny, not allow (after FIN-4115/3)
  Insert the allow row then the deny row. SELECT rights_class FROM search_input_publisher_lineage_v1 FINAL WHERE content_type = brief AND content_id = brief:fin4115-fixture. Assert exactly one row with rights_class deny. Assert on the returned row only; never on the query string and never on the absence of an exception.
- **FIN-4115/5** Prove the fixture fails on the pre-#359 shape by mutating _as_utc (after FIN-4115/4)
  Temporarily change projection._as_utc to return its input unchanged (the pre-#359 identity) and run the fixture: it must FAIL because the stale allow survives the lower-stamped rebuild. Restore the code. This is the repo guard-proof convention: mutate real code and require the test to fail.
- **FIN-4115/6** Wire the fixture into CI and run the full gate (after FIN-4115/4, FIN-4115/5)
  Add tests/test_search_input_projection_engine.py to the pytest command in the search-reader-clickhouse-engine job in .github/workflows/ci.yml. Confirm the job minimal --with dependency set covers the import chain (projection imports app.db.clickhouse which imports httpx); extend it if needed. Run uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen pytest, then open a PR against dev and require CI green.

### Ticket body (claim, not truth)

Domain: [SEC Filings](<https://linear.app/joefinks/project/sec-filings-7a04c2cc5631>)

Repo: finks-clustering-service

Context: FIN-3481 was reopened by HQ-50 and widened to behavioural (deny-direction) coverage. Its finks-dagster half landed in [https://github.com/Finks-ai/finks-dagster/pull/413](<https://github.com/Finks-ai/finks-dagster/pull/413>). The motivating fail-open is in finks-clustering-service and is out of that repo, so it is split out here.

The fail-open (FIN-3368 defect #2, fixed pre-merge in PR #359): `clustering_prod.search_input_publisher_lineage_v1` is a `SharedReplacingMergeTree(..., computed_at)` ordered by `(content_type, content_id)`, so `computed_at` is the version key. The column is `DateTime64(3)` with no timezone, so a naive datetime is converted host-local by clickhouse-connect. A rebuild that writes a lower version leaves a stale `allow` in place after a rebuild meant to withdraw it; a stamp in the future makes a stale verdict read as fresh. The SQL is valid and raises nothing, so execution-only coverage cannot catch it.

Ask: a live-engine fixture on the briefs-arm projection's rights-verdict / `computed_at` version path that proves a rebuild withdrawing an `allow` actually withdraws it on the rows returned. Assert on returned rows, not on the query string and not on the absence of an exception.

Acceptance:

* The fixture fails against a build that stamps `computed_at` host-local or lower than the previous version (the pre-#359 behaviour).
* It passes on current trunk.
* It runs read-only against a live engine, alongside the existing live-engine gates.

Split out of FIN-3481 ([FIN-3481](https://linear.app/joefinks/issue/FIN-3481/publication-store-sql-is-proven-only-against-a-fake-client-add-live)) by factory (operator: niko).
