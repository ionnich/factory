# Dispatch 20260928-195821-fin-3481

Staged 2026-09-28T19:58:21.273449Z by operator. This file is immutable (`chflags uchg`); factory.db holds its sha256.

## Repos

- Finks-ai/finks-dagster @ trunk `631030d31e2e674de852ee3090faa24e01424121`

## Rules

- Work only the tickets below. One PR per ticket, against the repo's trunk.
- Report through `factory card claim|comment|done|block <run_id> <ID>`; `done` needs the PR URL.
- Never write to Linear. Reconcile does that after the dispatch closes.
- The ticket body is a claim; the code and DB are truth. If the verdict below no longer holds, `factory card block` with the evidence instead of forcing a change.

## FIN-3481: Publication-store SQL is proven only against a fake client; add live-engine execution coverage

- Linear: https://linear.app/joefinks/issue/FIN-3481/publication-store-sql-is-proven-only-against-a-fake-client-add-live (Todo, assignee aaron.gumapac@finks.ai)
- Repo: Finks-ai/finks-dagster (context sec-filings), trunk `631030d31e2e674de852ee3090faa24e01424121`
- Verdict: valid — The publication stores' SELECTs are still proven only against an in-memory fake client; no live-engine execution harness or coverage is committed. The FIN-3366 defects are fixed in store code, but the durable live-engine execution + behavioural fixtures this ticket asks for do not exist yet.
- Evidence:
  - `{"note": "ClickHousePublicationOrchestrationStore (line 1743) and base ClickHouseFilingPublicationStore (publication.py:1045) hold ~13 statically-composable SELECTs; FINAL-alias and NOT IN fixes present, no live-engine execution coverage", "path": "finks_dagster/defs/filings/publication_orchestration.py", "sha": "6cf8ce1f2ff82d46142b3ba31ce3c2428a7901ab", "type": "file"}`
  - `{"note": "line 1188 exercises the store through a fake client that records the statement without executing it; no live-engine/integration test executes the publication-store SELECTs", "path": "tests/defs/filings/test_publication_orchestration.py", "sha": "6cf8ce1f2ff82d46142b3ba31ce3c2428a7901ab", "type": "file"}`

### Ticket body (claim, not truth)

Domain: [SEC Filings](<https://linear.app/joefinks/project/sec-filings-7a04c2cc5631>)

Repo: finks-dagster

Honors: deny-direction publication and rights-withdrawal integrity recorded in this ticket

Two production waves have now been burned by ClickHouse SQL defects that the existing tests structurally cannot catch. This is the root cause of that class, not another instance of it.

**The gap.** `tests/defs/filings/test_publication_orchestration.py:585` exercises `select_publication_candidates` through a fake client that records the SQL string and returns `result_rows: []`. It can assert on the string but never executes it, so any query that is syntactically invalid, or valid-but-unsupported by the engine, passes green. That pattern covers the whole `ClickHousePublicationOrchestrationStore`, not just the two lines fixed under FIN-3366.

**What it let through** (both found live, mid-production-wave, in `publication_orchestration.py`):

* `:1590` — `filing_event FINAL AS event`. ClickHouse requires the alias BEFORE `FINAL`; the query is a hard `Code: 62 SYNTAX_ERROR`. Hits every batch mode.
* `:1587` — same alias defect in the `unprocessed_only` guard, but reordering alone still fails: ClickHouse rejects the correlated `NOT EXISTS` subquery referencing the outer table (`Code: 1: Resolved identifier 'event.event_id' in parent scope`). Needed a `NOT IN` rewrite. This one is reached only on the reconciliation path — i.e. the S0 backfill — so a single-error patch of `:1590` would have shipped a green canary straight into a broken backfill.

**Known precedent, already burned once:** FIN-2820 documented exactly this (`scripts/fin2820_recompute/queries.py:23` — "alias BEFORE FINAL is load-bearing, not stylistic ... no in-memory fake client can catch that") and added string-assert guards in `tests/fin2820_recompute/test_queries.py:99`, `tests/defs/evidence_fabric/test_store_fixed_strings.py:392`, `tests/defs/evidence_fabric/test_sources.py:70`. The publication orchestration store never got the same treatment. String guards are a floor, not a fix — they would have caught defect A and missed defect B.

**Asset to keep:** the FIN-3366 specialist built a working harness that extracts every statically-composable SELECT in a module and executes each against a live engine read-only. That harness is what found defect B. It should land as a maintained repo script rather than being discarded with the session.

---

## HQ-50 (operator, 2026-09-08): reopened

Supersedes the HQ-44 "close without planning" disposition retained at the foot of this ticket. Operator ruling verbatim: *"Revisit FIN-3481 now that a rights-surface fail-open is demonstrated. FIN-3481 comes off the HQ-44 'close without planning' disposition; scope and sequencing are yours as domain owner."*

### Domain-owner scoping decision — the ticket as written would NOT prevent the motivating failure

This is the load-bearing judgement, recorded so it is not re-litigated.

The original acceptance catches **execution** defects: syntax errors and engine-unsupported constructs. Those fail LOUDLY — the query raises and the wave stops. Both FIN-3366 defects were of that kind.

A rights-surface fail-open is a different failure: **valid SQL that returns the wrong answer.** It does not raise. Live-engine execution coverage would run it, get rows back, and report green. Landing this ticket as originally scoped and declaring the fail-open class addressed would produce a gate that cannot fail on the very case that reopened it — the same emptiness recorded in FIN-3522, and in FIN-3498 whose tests encoded an unsafe sequence as correct.

Scope therefore widens along exactly one axis:

* **Execution coverage** (original scope, unchanged) — statically-composable SELECTs in `ClickHousePublicationOrchestrationStore` and `ClickHouseFilingPublicationStore` execute against a live engine read-only; fails on syntax and engine-unsupported constructs.
* **Behavioural coverage (NEW)** — for any query whose result gates rights, servability, or publication eligibility, a live-engine fixture asserts the DENY direction: rows that must not be served are proven not served. The assertion must be on returned rows, not on the query string and not on absence of exception.

Rationale for the widening: an execution-only check on a rights query is itself a fake-green. This ticket exists because a test that could not fail was trusted.

### The demonstrated fail-open — ANSWERED, and independently verified

HQ-50's justification cites a demonstrated rights-surface fail-open. It is **FIN-3368's** `computed_at` **version-stamp defect**, producer-side, in this domain's own `finks-clustering-service` briefs-arm projection build — defect #2 of three found only by live-engine execution. Fixed pre-merge in PR #359, which is why current arms read clean.

**Mechanism, verified against production 2026-09-08 rather than accepted on report:**

* `clustering_prod.search_input_publisher_lineage_v1` is `SharedReplacingMergeTree('...', '{replica}', computed_at) ORDER BY (content_type, content_id)` — so `computed_at` is the **version key**, not merely a build stamp.
* The column is `DateTime64(3)` with **no timezone**, so a naive datetime is converted host-local by clickhouse-connect.
* Consequence: a rebuild writing a **lower** version leaves a stale `allow` **surviving a rebuild intended to withdraw it**. A stamp landing in the **future** makes a stale verdict read as fresh.

That is a genuine fail-open: the withdrawal silently does not take. It is exactly the class live-engine *execution* coverage cannot catch, because the SQL is valid and raises nothing — which is why it belongs to this ticket's widened axis.

**Fixture target is therefore known and specific:** the projection's rights-verdict/`computed_at` version path in `finks-clustering-service`. The behavioural fixture must assert that **a rebuild which withdraws an** `allow` **actually withdraws it on returned rows** — not that the rebuild ran, and not that the query executed.

Producer half is this domain's. No consumer-side owner is implicated by the recorded evidence.

**Standing guard already in place:** the manager crib now runs `rights version-stamp not future` against production (fails if any verdict row carries `computed_at > now('UTC')`); current reading is 0. That guards one direction cheaply and continuously, but it is **not a substitute** for the fixture — it cannot detect the lower-version case, where the stale row simply survives. Only a rebuild-and-assert-withdrawal fixture covers that.

### Sequencing

Not blocking any active delivery. The publication pipeline is parked (FIN-3504 — acquisition runs, event minting stopped, no enrichment instigator, schedule not controllable from the manager host), so publication-store SQL is not executing in anger.

Sequenced **ahead of any re-enablement of the publication schedule or a new backfill** — that is the moment this SQL next runs against production, and it is exactly when both prior burns occurred. Not sequenced ahead of it as a standing hardening lane.

**Second sequencing trigger, added 2026-09-08:** the briefs/stories projection is frozen at build stamp `2026-09-05 07:22:07.858` (85h+ and drifting; activation was a one-shot manual build with no instigator) while Joe continues publishing. **The next rebuild of that projection is precisely when the** `computed_at` **version-stamp class would bite again**, so the behavioural fixture should land before the projection is rebuilt, not after.

## Acceptance

* A live-engine (or equivalently faithful) execution check covers the statically-composable queries of `ClickHousePublicationOrchestrationStore` and `ClickHouseFilingPublicationStore`, failing on syntax and engine-unsupported constructs — not just on string shape.
* **Any query gating rights, servability, or publication eligibility additionally carries a live-engine behavioural fixture asserting the deny direction on returned rows.**
* The SELECT-extraction/execution harness is committed as a supported script with usage docs, read-only by construction.
* Dynamically-composed queries (the two that string-assert tests structurally cannot reach) are covered explicitly.
* Existing FIN-2820-style string guards are retained as the cheap first line; the new coverage is additive.
* Runs in CI, or is documented as a pre-deploy gate for the publication control plane, whichever the repo's ClickHouse availability supports.

Related: FIN-3366 (both defects found and fixed mid-wave), FIN-2820 (same defect class, prior burn), FIN-3522 (a gate whose verdict turns on a race certifies nothing), FIN-3504 (pipeline parked — sequencing gate).

---

**Superseded — HQ-44 disposition (operator, 2026-09-04): "Close without planning."** Retained for audit. That disposition said the ticket re-enters only if it becomes load-bearing for a real delivery, on that delivery's own merits. HQ-50 is that re-entry.
