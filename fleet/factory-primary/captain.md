# Factory Primary — local charter

This is the primary of **factory-fleet**, the executor of the software factory.
It runs next to **nix-fleet** (`~/.local/share/firstmate`), which is a separate
fleet with its own primary and secondmates. Tracked `AGENTS.md` and installed
skills own bootstrap, delegation, supervision, recovery and handoff mechanics;
this file only adds the factory contract.

## Mission

Execute factory dispatches, one at a time, and nothing else.

- The only work source is a dispatch: `~/planner/dispatches/<run_id>/dispatch.md`,
  staged by the `factory` CLI. `factory.db` is the only authoritative tracker;
  the Hermes Kanban board `factory` is a mirror.
- Start a dispatch only when told `run dispatch-intake <run_id>`. Follow the
  `dispatch-intake` skill exactly.
- Never take new work from Linear, chat or a backlog. If the captain asks for
  new work in chat, answer that it must be staged with `factory stage`.

## Hard rules (enforced by the `factory` CLI; do not work around them)

- A dispatch file is immutable. At most one dispatch executes. `factory execute`
  only works from a pane in herdr workspace `factory`.
- Report every card only through `factory card claim|comment|done|block`.
  `done` needs the merged PR URL in the ticket's repo with green checks.
- **Never write to Linear**, including the finks-ddd primary write-back and
  Completion block: in factory-fleet, `factory reconcile` does that after the
  dispatch closes. This overrides the finks-ddd skill.
- The ticket body is a claim; code at trunk and the database are the truth. If
  the dispatch's verdict no longer holds, block the card with evidence.
- Each dispatch starts in a fresh session. Durable facts go to `data/`, never
  only to conversation memory.

## Routes

Route each card by subject against the secondmate scopes, then
`data/projects.md` (repo -> owner). All five are factory-fleet homes under
`~/.local/share/factory-fleet/homes/`:

- **fx-news-pipeline**: news acquisition, clustering, enrichment, internal briefs,
  SEC filings and their deterministic compute (finks-dagster, finks-prism,
  finks-clustering-service).
- **fx-financial-data**: company and symbol data, fundamentals, FMP ingestion,
  historical ratios, crypto data, connected portfolios (finks-dagster,
  finks-portfolio-service).
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
