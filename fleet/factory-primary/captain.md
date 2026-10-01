# Factory Primary — local charter

This is the primary of **factory-fleet**, the executor of the software factory.
It runs next to **nix-fleet** (`~/.local/share/firstmate`), which is a separate
fleet with its own primary and secondmates. Tracked `AGENTS.md` and installed
skills own bootstrap, delegation, supervision, recovery and handoff mechanics;
this file only adds the factory contract.

## Mission

Execute factory dispatches and nothing else. Several dispatches may be in
flight at once, bounded by the factory's `max_parallel` (default 2) and by
conservative reservations; each dispatch is one `run_id` with its own state.

- The only work source is a dispatch: `~/factory/dispatches/<run_id>/dispatch.md`,
  staged by the `factory` CLI. `factory.db` is the only authoritative tracker;
  the Hermes Kanban board `factory` is a mirror.
- Start a dispatch only when told `run dispatch-intake <run_id>`. Follow the
  `dispatch-intake` skill exactly. A dispatch whose `## Runs in` names a domain
  lead goes straight to that lead's pane, not to you; you get the ones with no
  single owner, and the ones whose lead has no live pane (spawn or wake it, then
  route the cards as usual).
- Never take new work from Linear, chat or a backlog. If the captain asks for
  new work in chat, answer that it must be staged with `factory stage`.

## Hard rules (enforced by the `factory` CLI; do not work around them)

- A dispatch file is immutable. Dispatch is bounded, not single: `max_parallel`
  (default 2) plus repo, route, pane and hierarchical-resource reservations. The
  CLI reserves a run's launch slot and resource claims atomically before it
  sends; `factory execute` enforces the same guards and only works from a pane
  in herdr workspace `factory`.
- Report every card only through `factory card claim|comment|done|block`.
  `done` needs the merged PR URL in the ticket's repo with green checks.
- **Never write to Linear**, including the finks-ddd primary write-back and
  Completion block: in factory-fleet, `factory reconcile` does that after the
  dispatch closes. This overrides the finks-ddd skill.
- The ticket body is a claim; code at trunk and the database are the truth. If
  the dispatch's verdict no longer holds, block the card with evidence.
- Each dispatch starts in a fresh session. Durable facts go to `data/`, never
  only to conversation memory.

## Pinned briefs and per-run state

- A dispatch may be backed by an approved Strategy brief. For brief-backed runs
  `dispatch.md` carries the compiled brief intent (title, outcome, acceptance,
  scope, exclusions, decisions, dependencies, resources, risks, evidence) plus
  the captured source provenance — never the raw Linear narrative. Plan and
  verdict against the brief; do not re-read the source ticket to reconstruct
  intent.
- Intent, version and amendment conflicts are raised against the brief, not
  guessed. If the brief no longer matches trunk or its sources changed since
  capture (needs-amendment), block the card with evidence and ask the captain;
  never silently rebase or adopt a changed version.
- Fresh code/data evidence is still required for every card. Stale evidence
  refuses execution and asks for a recheck/replan, never a blanket bypass.
- Each `run_id` has its own launch reservation (`reserved`/`sent`/`uncertain`),
  resource claims and pane. Never `/new` a pane twice after an uncertain send;
  an uncertain launch is shown to the operator, not replayed or auto-expired.
  Release a reservation only when definitely unsent, or terminal and executor-safe.

## Routes

Route each card by subject against the secondmate scopes, then
`data/projects.md` (repo -> owner). All six are factory-fleet homes under
`~/.local/share/factory-fleet/homes/`:

- **fx-news-pipeline**: news acquisition, clustering, enrichment, internal briefs,
  SEC filings and their deterministic compute (finks-dagster, finks-prism,
  finks-clustering-service).
- **fx-financial-data**: company and symbol data, fundamentals, FMP ingestion,
  historical ratios, crypto data, connected portfolios (finks-dagster,
  finks-portfolio-service).
- **fx-finks-data**: the Finks Data platform (Linear Domain "Finks Data"):
  Dagster platform and sandbox harness, ClickHouse master tables and views,
  raw-data ingestion health (CoinGecko etc.), the data-platform contract
  (finks-dagster). FMP Ingestion and Historical Ratios stay fx-financial-data.
- **fx-personalization**: personalization profile and per-user Insights
  generation, ranking and serving (finks-insights-service,
  finks-personalization-service).
- **fx-commercialize-api**: enterprise external API surfaces, search API,
  entitlements, metering (finks-api).
- **fx-core-rs**: finks-core-rs only; repo-shaped domain, captain holds merge.

`finks-dagster` is shared: route by subject. A repo with no factory-fleet owner
(for example finks-overwatch) or a card no route fits: block the card with the
reason; never invent an owner and never hand work to a nix-fleet home.

## Coexisting with nix-fleet

- Never read or write under `~/.local/share/firstmate/`, never message a
  nix-fleet pane, never adopt a nix-fleet workspace or task.
- Every `bin/fm-*` call runs with `FM_HOME` and `FM_ROOT_OVERRIDE` set to this
  home. An inherited nix-fleet `FM_HOME` writes into the wrong fleet.
- Crew task ids start with `fx-` so `/tmp/fm-<id>` never collides.

## Known routing uncertainties

- Commercialize API has unresolved repository routes for API Dashboard,
  Research API and Screener API.
- Personalization may touch `finks-overwatch`, which factory-fleet does not own.

Block the card and state the question rather than guessing.
