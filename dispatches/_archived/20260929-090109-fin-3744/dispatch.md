# Dispatch 20260929-090109-fin-3744

Approved 2026-09-29T09:14:49.779005Z by user:dashboard. This file is immutable (`chflags uchg`); factory.db holds its sha256.

## Repos

- Finks-ai/finks-dagster @ trunk `3a744be31295bba50919d770f475491f295a9ec9`

## Rules

- Work only the tickets below. One PR per ticket, against the repo's trunk.
- Follow each ticket's plan in dependency order. Operator notes and answered questions are binding and override the plan and the ticket body; if one cannot be followed, `factory card block` with why.
- Report through `factory card claim|comment|done|block <run_id> <ID>`; `done` needs the PR URL. Name the step id (e.g. FIN-1/2) in comments.
- A choice only the captain can make: `factory decide ask` (options + your recommendation), then keep working on other tickets; the answer arrives in this session.
- Never write to Linear. Reconcile does that after the dispatch closes.
- The ticket body is a claim; the code and DB are truth. If the verdict below no longer holds, `factory card block` with the evidence instead of forcing a change.

## Theme: Landing-verify support for joe_stories in the sandbox harness

One ticket: finish the third leg of letting a joe_stories-writing asset be proven by a sandbox_cli run plus a landing verify. Items 1-2 (posthog in SANDBOX_RESOURCE_KEYS, JOE_STORIES_DATABASE isolation rebind) already landed on the JOE-373 PR; only the verify target and its sandbox database remain.

## Answered questions (binding)

- On FIN-3744/2: How should the sandbox seed the stories roster the asset reads? **Synthetic stub roster**: create a minimal stories table (post_id, slug, is_published) with a few stub rows; fully isolated, no shared reads; matches the existing _bootstrap_stories integration-test pattern (user:dashboard (approved with the recommendation))
- On FIN-3744/1: Should the joe_stories schema live in a dedicated sandbox database or the medallion one? **Dedicated joe_stories_sandbox_<id> database**: mirrors prod (joe_stories is its own database) and the filings/insights precedent; requires widening the story_popularity migration throwIf allow-list to also admit joe_stories_sandbox_% (user:dashboard (approved with the recommendation))

## FIN-3744: Chore: Let the sandbox harness reach joe_stories so joe_stories-writing assets can be proven

- Linear: https://linear.app/joefinks/issue/FIN-3744/chore-let-the-sandbox-harness-reach-joe-stories-so-joe-stories-writing (Todo, assignee aaron.gumapac@finks.ai)
- Repo: Finks-ai/finks-dagster (context finks-data), trunk `3a744be31295bba50919d770f475491f295a9ec9`
- Verdict: valid — Landing-verify support for joe_stories remains unbuilt at trunk: SandboxResources has no joe_stories_database field and _resolve_verify_target only maps insights/filings, so a joe_stories-writing asset still cannot be proven via sandbox_cli. Items 1-2 (posthog in SANDBOX_RESOURCE_KEYS, JOE_STORIES_DATABASE rebind in create_sandbox) are done; item 3 is the open gap.
- Evidence:
  - `{"note": "VERIFY_DATABASE_QUALIFIERS=(\"insights\",\"filings\") and _resolve_verify_target handle only those two qualifiers; no joe_stories branch", "path": "finks_dagster/testing/sandbox_cli.py", "sha": "b0398c2e02e292b7a115e2e01208f7539af07b9b", "type": "file"}`
  - `{"note": "SandboxResources has insights_database/filings_database but no joe_stories_database; create_sandbox comment explicitly tracks landing-verify support on FIN-3744", "path": "finks_dagster/testing/sandbox.py", "sha": "b0398c2e02e292b7a115e2e01208f7539af07b9b", "type": "file"}`

### Plan

- **FIN-3744/1** Mint the fourth sandbox database and add it to SandboxResources
  Add JOE_STORIES_SANDBOX_DB_PREFIX (joe_stories_sandbox_) next to the filings prefix; add a joe_stories_database field to SandboxResources; create the database in create_sandbox with the same safety asserts as insights/filings; update the module docstring that says the sandbox owns three databases.
- **FIN-3744/2** Provision the joe_stories schema: apply story_popularity and seed a stories roster (after FIN-3744/1)
  Create the story_popularity table by applying db/migrations_joe_stories/20260915000001_joe373_story_popularity.sql into the new database (widening its up/down throwIf allow-list per the question), then create a minimal stories table and seed it per the roster question.
- **FIN-3744/3** Point JOE_STORIES_DATABASE at the new database and drop it on teardown (after FIN-3744/1)
  Change create_sandbox to rebind JOE_STORIES_DATABASE to joe_stories_database instead of the medallion db_name; add a drop step and env restore in destroy_sandbox with prefix safety checks.
- **FIN-3744/4** Add the joe_stories qualifier to the landing-verify resolver (after FIN-3744/1)
  Add joe_stories to VERIFY_DATABASE_QUALIFIERS and a branch in _resolve_verify_target mapping it to sb.joe_stories_database; update the --verify-table metavar and help text in sandbox_cli.py.
- **FIN-3744/5** Cover the fourth database in cleanup and list (after FIN-3744/1)
  Add JOE_STORIES_SANDBOX_DB_PREFIX to cmd_cleanup --sandbox-id drop loop and its GC LIKE query, and to cmd_list query, so a --keep run or interrupted teardown cannot leak the database.
- **FIN-3744/6** Re-pin tests and docs; run the acceptance command (after FIN-3744/1, FIN-3744/2, FIN-3744/3, FIN-3744/4, FIN-3744/5)
  Update tests (sandbox_cli verify-qualifier/resolver assertions, story_popularity isolation-wiring field assertion, the migration allow-list test for the widened prefix) and README.md lines 281 and 289-304 (qualifier set, limitation 4). Then run the acceptance command and confirm it creates a sandbox and prints a landing verify.

### Ticket body (claim, not truth)

Domain: [Finks Data](<https://linear.app/joefinks/project/finks-data-37583a21190b>)
Repo: `finks-dagster`

**Context:** `AGENTS.md:50` ("Definition of proven") admits exactly one proof shape: a `sandbox_cli` run plus a landing verify. That shape is unreachable for any asset that writes to `joe_stories` / `joe_stories_dev`, which [JOE-373](https://linear.app/joefinks/issue/JOE-373/materialize-per-story-engagement-counts-views-reads-from-posthog-into) is the first to do. Everything else finks-dagster owns in those databases (`stories_search`, `briefs_search`, `search_corpus_view`) is fed by a refreshable materialized view running inside ClickHouse, so no asset had ever needed a writer there and the gap had never been hit.

**Two of the four are now done** on the [JOE-373](https://linear.app/joefinks/issue/JOE-373/materialize-per-story-engagement-counts-views-reads-from-posthog-into) PR (`4646cf2`), including the one that actually mattered. What is left is the landing verify. The other two [JOE-373](https://linear.app/joefinks/issue/JOE-373/materialize-per-story-engagement-counts-views-reads-from-posthog-into) already worked around.

**Change:**

1. **DONE** — `"posthog"` added to `SANDBOX_RESOURCE_KEYS` (`sandbox_cli.py:75`). `SandboxResources` gained a matching `posthog` field in the same change; without both, `run-job` refused the job before a sandbox existed.
2. **DONE** — the isolation hazard, where a sandboxed run wrote into the shared `joe_stories_dev`. `create_sandbox` now rebinds `JOE_STORIES_DATABASE` to the sandbox database and `destroy_sandbox` restores it, mirroring the storylines block directly above it. Measured with `ENV=prod`: outside a sandbox `joe_stories`, inside `ck_sandbox_<id>`, restored on teardown with no env residue.
3. **REMAINING — the landing verify.** It needs a sandbox `joe_stories` database to point at, which does not exist. `_resolve_verify_target` (`sandbox_cli.py:149-169`) maps a qualifier to a **field on the sandbox object** — `insights` to `sb.insights_database`, `filings` to `sb.filings_database` — so adding `"joe_stories"` to `VERIFY_DATABASE_QUALIFIERS` (`:81`) on its own resolves to nothing. Its own error message is the giveaway: "the landing verify never reads shared state". The remaining change is a fourth sandbox database, mirroring what filings and insights already do:
   * a `joe_stories_database` field on `SandboxResources`, and the database minted in `create_sandbox` and dropped in teardown
   * `JOE_STORIES_DATABASE` pointed at THAT database rather than at the medallion sandbox db, which is where item 2 currently points it
   * `story_popularity` applied into it, plus a seeded `stories` roster — see below
   * then the qualifier entry and its `_resolve_verify_target` branch

`tests/testing/test_sandbox_cli_run_job.py:1004-1024` asserts real-job verdicts against `SANDBOX_RESOURCE_KEYS` and will need re-pinning. `README.md:281,289-304` names the qualifier set and the unconfined resolvers explicitly and goes stale on the same change.

**Already handled, no action needed:**

* *Why the isolation fix was urgent, for the record.* `resolve_stories_database` (`defs/checks/stories_symbols_coverage.py:177`) is a reader helper with no environment override. An asset built on it writes to the shared `joe_stories_dev` from inside a sandbox the CLI reports as confined. [JOE-373](https://linear.app/joefinks/issue/JOE-373/materialize-per-story-engagement-counts-views-reads-from-posthog-into) closed this in its own module with a `JOE_STORIES_DATABASE` override that always wins, rather than changing the harness. That module-level override is still what does the resolving, but `create_sandbox` now sets it, so the protection is in the harness rather than left to each writer to remember.
* *Schema provisioning.* Nothing creates `story_popularity` inside a sandbox; `--schema-from prod` mirrors `ck_*` only. [JOE-373](https://linear.app/joefinks/issue/JOE-373/materialize-per-story-engagement-counts-views-reads-from-posthog-into) works around it by applying the migration file directly.

**The hard part, and why it has no precedent.** `insights` gets `bootstrap_insights_schema` and `filings` gets `apply_filings_schema`, so both can mint a sandbox database with a usable schema. `story_popularity` is ours and its migration applies cleanly, but the asset also READS the published roster from `stories` in the same database — and `stories` is tool-joe's table, not one we migrate. So a sandbox `joe_stories` database has to be seeded with a roster from somewhere before `run-job` can land a single row. Copying the real roster read-only is what the [JOE-373](https://linear.app/joefinks/issue/JOE-373/materialize-per-story-engagement-counts-views-reads-from-posthog-into) proof script does by hand; whether that belongs in the harness is the open design question, and is why this is a day rather than a line.

**Assumptions (defaults taken, reversible):** the isolation hazard was the urgent half and is closed, so what is left is a standards gap rather than a safety one. Deprioritise or decline it freely — nothing ships behind it.

**Acceptance:** `uv run python -m finks_dagster.testing.sandbox_cli run-job --job story_popularity_job --verify-table joe_stories.story_popularity --verify-pk post_id --evidence` creates a sandbox rather than refusing, and prints a landing verify.

**Why the** `blockedBy` **edge, given the PR is green:** the edge is about proof, not delivery. [JOE-373](https://linear.app/joefinks/issue/JOE-373/materialize-per-story-engagement-counts-views-reads-from-posthog-into) can merge and ship without this: its PR is green and carries a live landing verify against real PostHog and the real published roster, obtained by driving the asset at a sandbox database directly. What it cannot do until this lands is satisfy `AGENTS.md:50`, which admits a `sandbox_cli` run as the only proof shape. So the edge is there to stop that ticket reading as Done on this repo's own standard while its proof path is unreachable, not to say the work is stalled.
