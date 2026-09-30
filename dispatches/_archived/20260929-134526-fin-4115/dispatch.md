# Dispatch 20260929-134526-fin-4115

Approved 2026-09-30T03:08:17.172553Z by factory:auto (you were silent after it reached you). This file is immutable (`chflags uchg`); factory.db holds its sha256.

## Repos

- Finks-ai/finks-clustering-service @ trunk `b2e66402f6784ec16b85525faee8911df15b4a4f`

## Rules

- Work only the tickets below. One PR per ticket, against the repo's trunk.
- Follow each ticket's plan in dependency order. Operator notes and answered questions are binding and override the plan and the ticket body; if one cannot be followed, `factory card block` with why.
- Report through `factory card claim|comment|done|block <run_id> <ID>`; `done` needs the PR URL. Name the step id (e.g. FIN-1/2) in comments.
- A choice only the captain can make: `factory decide ask` (options + your recommendation), then keep working on other tickets; the answer arrives in this session.
- Never write to Linear. Reconcile does that after the dispatch closes.
- The ticket body is a claim; the code and DB are truth. If the verdict below no longer holds, `factory card block` with the evidence instead of forcing a change.

## Theme: Prove the search-input rights rebuild withdraws an allow on returned rows

One ticket adds a live-engine behavioural fixture for the briefs-arm rights verdict: it writes an allow then a deny for one content_id and asserts the deny is the row the serving view returns, closing the pre-#359 fail-open where a lower computed_at stamp leaves a stale allow standing after a rebuild meant to withdraw it.

## Answered questions (binding)

- On FIN-4115/2: How should the fixture write the two verdict versions while the ticket also requires it to run read-only against a live engine? **Real table, synthetic content_id plus cleanup**: inserts an allow then a deny row into search_input_publisher_lineage_v1 under a reserved content_id, reads the FINAL view, then deletes both rows; proves the actual serving table version path and returned rows (factory:auto (you were silent after it reached you) (approved with the recommendation))

## FIN-4115: Behavioural fixture: a rebuild that withdraws an allow must withdraw it on returned rows (computed_at version path)

- In this dispatch: Add a live-engine behavioural fixture on the briefs-arm computed_at version path proving a rebuild that withdraws an allow withdraws it on the rows the serving view returns.
- Linear: https://linear.app/joefinks/issue/FIN-4115/behavioural-fixture-a-rebuild-that-withdraws-an-allow-must-withdraw-it (Todo, assignee none)
- Repo: Finks-ai/finks-clustering-service (context news-clustering), trunk `b2e66402f6784ec16b85525faee8911df15b4a4f`
- Verdict: valid — The computed_at version path and rights verdict remain in code, and the pre-#359 fail-open (host-local/lower stamp leaves a stale allow) is documented in _as_utc; no live-engine fixture yet proves a rebuild withdrawing an allow actually withdraws it on returned rows.
- Evidence:
  - `{"note": "_as_utc (lines 124-150) documents the fail-open: naive/lower computed_at stamp leaves a stale allow after a rebuild meant to withdraw it; to_rows stamps computed_at as the ReplacingMergeTree version", "path": "app/search_input/projection.py", "sha": "b2e66402f6784ec16b85525faee8911df15b4a4f", "type": "file"}`
  - `{"note": "search_input_publisher_lineage_v1 is ReplacingMergeTree(computed_at) ORDER BY (content_type, content_id); computed_at is the version key the fixture must exercise", "path": "migrations/054_search_input_projection_v1.sql", "sha": "b2e66402f6784ec16b85525faee8911df15b4a4f", "type": "file"}`
  - `{"note": "live serving table confirmed SharedReplacingMergeTree sorting on content_type, content_id; no fixture asserts withdraw-on-returned-rows", "query": "SELECT engine, sorting_key FROM system.tables WHERE name = 'search_input_publisher_lineage_v1' LIMIT 5", "result_sha256": "a57ac0915b62b891bdae77418e274117a21e53c049dbcfe8c7a3b195298f4583", "type": "sql", "witness": "clickhouse-serving", "witness_log_id": 85}`

### Plan

- **FIN-4115/1** Confirm the fixture home and live connection helper
  Read the existing live gate test_live_clickhouse_keeps_the_recorded_fin_3522_dev_build_split_by_arm in tests/test_search_input_projection_sql.py and the scripts/run_migration.py get_clickhouse_uri and parse_uri helpers; settle whether the new fixture lives in that file or a new tests/test_search_input_projection_live.py, gated behind FINKS_LIVE_CLICKHOUSE=1.
- **FIN-4115/2** Add the withdraw fixture asserting on returned rows (after FIN-4115/1)
  Build an allow row then a deny row for the same synthetic content_id via projection.to_rows with the deny stamp strictly newer through _as_utc, write both, read v_search_input_lineage_current FINAL for that content_id, and assert the returned rights_class is deny with no allow row present.
- **FIN-4115/3** Prove the guard fails on the pre-#359 shape (after FIN-4115/2)
  Mutate _as_utc or the stamp path to drop normalization or write a lower version, run the fixture and confirm it fails with the stale allow surviving, then restore; per the repo rule a guard is proven by making the test fail on real code.
- **FIN-4115/4** Make the fixture non-destructive and upstream read-only (after FIN-4115/2)
  Use a reserved synthetic content_id and delete the fixture rows in a finally block; assert the fixture writes only the lineage table and touches no upstream table such as briefs, feeds, or the registry.
- **FIN-4115/5** Run the full gate and the live fixture (after FIN-4115/3, FIN-4115/4)
  Run uv run --frozen ruff check . then ruff format --check . then pytest with the unit suite green and no live credentials, and run the fixture manually with FINKS_LIVE_CLICKHOUSE=1 and FINKS_LIVE_CLICKHOUSE_ENV=dev against the dev engine.
- **FIN-4115/6** Open PR against dev; CI green (after FIN-4115/5)
  Target dev per the promotion convention; confirm CI is green and note the live fixture skips in CI without credentials and is exercised manually against dev.

### Ticket body (claim, not truth)

Domain: [SEC Filings](<https://linear.app/joefinks/project/sec-filings-7a04c2cc5631>)

Repo: finks-clustering-service

Context: [FIN-3481](https://linear.app/joefinks/issue/FIN-3481/publication-store-sql-is-proven-only-against-a-fake-client-add-live) was reopened by HQ-50 and widened to behavioural (deny-direction) coverage. Its finks-dagster half landed in [https://github.com/Finks-ai/finks-dagster/pull/413](<https://github.com/Finks-ai/finks-dagster/pull/413>). The motivating fail-open is in finks-clustering-service and is out of that repo, so it is split out here.

The fail-open ([FIN-3368](https://linear.app/joefinks/issue/FIN-3368/feature-publish-rights-cleared-brief-and-story-search-inputs) defect #2, fixed pre-merge in PR #359): `clustering_prod.search_input_publisher_lineage_v1` is a `SharedReplacingMergeTree(..., computed_at)` ordered by `(content_type, content_id)`, so `computed_at` is the version key. The column is `DateTime64(3)` with no timezone, so a naive datetime is converted host-local by clickhouse-connect. A rebuild that writes a lower version leaves a stale `allow` in place after a rebuild meant to withdraw it; a stamp in the future makes a stale verdict read as fresh. The SQL is valid and raises nothing, so execution-only coverage cannot catch it.

Ask: a live-engine fixture on the briefs-arm projection's rights-verdict / `computed_at` version path that proves a rebuild withdrawing an `allow` actually withdraws it on the rows returned. Assert on returned rows, not on the query string and not on the absence of an exception.

Acceptance:

* The fixture fails against a build that stamps `computed_at` host-local or lower than the previous version (the pre-#359 behaviour).
* It passes on current trunk.
* It runs read-only against a live engine, alongside the existing live-engine gates.

Split out of [FIN-3481](https://linear.app/joefinks/issue/FIN-3481/publication-store-sql-is-proven-only-against-a-fake-client-add-live) ([FIN-3481](https://linear.app/joefinks/issue/FIN-3481/publication-store-sql-is-proven-only-against-a-fake-client-add-live)) by factory (operator: niko).
