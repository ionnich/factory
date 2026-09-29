# factory: software factory control plane

Linear tickets in niko's domains are checked against code and data, grouped into reviewed dispatches (cohorts of related tickets), executed by
factory-fleet, and written back to Linear. `~/.hermes/factory.db` is the only authoritative tracker.

```
ingest (cron) -> prune verdicts (cron) -> draft -> plan (cron agent) -> review: your notes -> approve -> handoff
  -> execute (factory-fleet) -> reconcile (cron) -> archive
propose (cron) drafts for `auto` repos, announces the review, and starts it after 2h unless you hold it
```

## Install

`./install.sh` (idempotent): venv, `factory` on PATH, Hermes scripts/skills/cron jobs (`factory-ingest`,
`factory-prune`, `factory-plan`, `factory-reconcile`, `factory-propose`, `factory-backup`), the `factory` plugin (dashboard tab +
chat tool), factory-fleet primary files.

One-time, by hand:

- Dashboard tab: root `~/.hermes/config.yaml` `plugins.enabled: [factory]`.
- Chat profile: `hermes profile create factory --clone --no-alias`, then in its `config.yaml`:
  `model.default deepseek-v4-pro`, `model.provider deepseek`, `plugins.enabled: [factory]`,
  `platform_toolsets.cli: [factory]`. Run `./install.sh` again to copy the plugin and `SOUL.md`.
- factory-fleet: see `fleet/`. Primary runs in herdr workspace `factory`. `handoff` starts it there with
  `fleet/launch-factory-primary.sh` when the workspace or agent is missing (reboot, crash, closed pane).

## Use

- Dashboard: Hermes dashboard, **Factory** tab: status per lifecycle stage, draft / review (plan tree, notes on
  any node, approve / hold / reject) / resolve flags, and throughput (`factory metrics`). A click there is the
  approval. UI source is React JSX in `hermes/plugins/factory/dashboard/src/index.jsx` (React comes from the
  dashboard SDK); `./install.sh` bundles it with `bun build` into the gitignored `dist/` and copies the plugin.
- Chat: `hermes -p factory`, also on the iPhone through Hermex (Bot Mode, factory profile). It shows drafts,
  takes notes ("note FIN-3788/2: …"), holds, rejects, and approves (Hermes approval prompt).
- Review: `factory stage` makes a **draft**. `factory-plan` (agent, every 10m) writes its plan tree: a theme,
  tickets nested under the ones they build on (misfits dropped with a reason), steps per ticket (`FIN-1/2`,
  nested `FIN-1/2.1`) with `depends_on` edges. You add notes to the dispatch (`root`), a ticket
  or a step; `factory draft approve` renders plan + notes into the immutable `dispatch.md`, where notes bind the
  executor. `draft hold` stops the auto-start clock; `draft reject` archives the draft as a record, and its
  tickets are not drafted again until their verdict changes. Approval re-checks every ticket: one that changed
  during review voids the draft.
- Autonomous: `factory-propose` (every 10m) drafts a cohort in `auto = true` repos: the top candidate plus those
  sharing its Linear Domain, then its repo (up to `stage.max_tickets`); the planner shapes it into a tree and may
  drop misfits; announces it once planned to the factory Bot Chat (Hermex), and approves it itself 2h later unless held.
  **Emergency** (no review window, a one-ticket dispatch): the ticket is Urgent in Linear (a person set it) and its verdict evidence
  touches at most 3 files. A person's draft is never started by the cron. It also hands off approved dispatches.
  Stop it with `hermes cron pause factory-propose`.
- Backups: `factory-backup` (03:00) writes `~/.hermes/factory/backups/factory-YYYY-MM-DD.db` (newest 14), and every
  schema migration first writes `factory-pre-vN.db`. Same disk: protects against bad writes, not disk loss.
- CLI: `factory status|tickets|candidates|stage|draft|handoff|propose|execute|card|reconcile|archive|metrics|backup`.

## Invariants (in code: `factory/schema.sql` triggers + CLI checks)

- A dispatch is born a draft and leaves review only approved (`approved_by` set) or rejected with a reason;
  plan steps and notes are writable only while draft (triggers). Once staged it is immutable (`chflags uchg` +
  sha256); at most one executes.
- `execute` only from a pane in herdr workspace `factory`; `handoff` resets the executor session (`/new`) first.
- Card `done` needs a merged PR in the ticket's repo with green checks.
- Only `reconcile` writes Linear. State changes need an unassigned-or-lead ticket (and, for verdicts, an unchanged
  `updatedAt`); otherwise comment + flag. The reconcile agent may only reword prose or downgrade apply -> flag.
- `stage` skips tickets named in nix-fleet backlogs or nix-fleet herdr workspace labels.
- A verdict is dispatched at most once (a blocked card needs the ticket to change first); `valid` verdicts expire
  after 7 days. Tickets in the team's review state (Ready for QA) are out of scope: they wait on a human.
- The reconcile gate flags an executing dispatch whose executor pane is gone or with no card activity for
  `executor.stuck_hours`.
