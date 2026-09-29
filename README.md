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

- Dashboard: Hermes dashboard, **Factory** tab, phone first, a stack of cards. **Needs you** is a deck of
  decisions: the question, the options with what each leads to, the recommended one marked ★ and why. Tap an
  option (a second tap confirms ones that start or stop work or write Linear; some ask for a reason), swipe
  right to take ★, left for later. **Dispatches** are cards; open one to see it as a flow of cards down a rail:
  dispatch → review → tickets → steps (branching where the plan branches) → results (PR, Linear writes), with
  each decision hanging off the node it's about. Tap a card to light up what it leads to; `+ note` on a draft's
  nodes. **Tickets** not in a dispatch are a list (ready / needs answer / checking / not for us), with the
  factory's recommended next group one tap away. UI source is React JSX in
  `hermes/plugins/factory/dashboard/src/index.jsx` (React and components come from the dashboard SDK);
  `./install.sh` bundles it with `bun build` into the gitignored `dist/` and copies the plugin.
- Chat: `hermes -p factory`, also on the iPhone through Hermex (Bot Mode, factory profile). It shows drafts,
  takes notes ("note FIN-3788/2: …") and answers decisions (weighty ones through the Hermes approval prompt).
- Decisions (`factory decide list|choose|ask`): every choice the factory needs from you is a decision with 2+
  options, what each leads to, and one recommended with why. Kinds: a draft's review (approve / hold / reject),
  planner questions, executor questions mid-run (`decide ask`; the answer is typed into its pane), a blocked
  ticket (write back / retry with guidance), a missing or quiet executor (restart / wait / stop), a held Linear
  write (apply anyway / skip / do it yourself).
- Review: `factory stage` makes a **draft**. `factory-plan` (agent, every 10m) writes its plan tree: a theme,
  tickets nested under the ones they build on (misfits dropped with a reason), steps per ticket (`FIN-1/2`,
  nested `FIN-1/2.1`) with `depends_on` edges, questions, and a review recommendation. You add notes to the
  dispatch (`root`), a ticket or a step, and answer questions; approving the review decision renders plan, notes
  and answers into the immutable `dispatch.md`, where they bind the executor (open questions take their
  recommendation). Hold stops the auto-start clock; reject archives the draft as a record, and its tickets are
  not drafted again until their verdict changes. Approval re-checks every ticket: one that changed during review
  voids the draft.
- Autonomous: `factory-propose` (every 10m) drafts a cohort in `auto = true` repos: the top candidate plus those
  sharing its Linear Domain, then its repo (up to `stage.max_tickets`); the planner shapes it into a tree and may
  drop misfits; announces it once planned to the factory Bot Chat (Hermex), and 2h later takes the review
  decision's recommendation unless you answered first.
  **Emergency** (no review window, a one-ticket dispatch): the ticket is Urgent in Linear (a person set it) and its verdict evidence
  touches at most 3 files. A person's draft is never started by the cron. It also hands off approved dispatches.
  Stop it with `hermes cron pause factory-propose`.
- Backups: `factory-backup` (03:00) writes `~/.hermes/factory/backups/factory-YYYY-MM-DD.db` (newest 14), and every
  schema migration first writes `factory-pre-vN.db`. Same disk: protects against bad writes, not disk loss.
- CLI: `factory status|tickets|candidates|stage|draft|decide|handoff|propose|execute|card|reconcile|archive|metrics|backup`.

## Invariants (in code: `factory/schema.sql` triggers + CLI checks)

- A dispatch is born a draft and leaves review only approved (`approved_by` set) or rejected with a reason;
  plan steps and notes are writable only while draft (triggers). Once staged it is immutable (`chflags uchg` +
  sha256); at most one executes.
- `execute` only from a pane in herdr workspace `factory`; `handoff` resets the executor session (`/new`) first.
- Card `done` needs a merged PR in the ticket's repo with green checks.
- Only `reconcile` writes Linear. State changes need an unassigned-or-lead ticket (and, for verdicts, an unchanged
  `updatedAt`); otherwise comment + held write. The reconcile agent may only reword prose or downgrade apply ->
  flag; a held write is re-applied only by a person (`approved_by`), and then the agent cannot hold it again.
- A decision has >= 2 distinct options (id, label, leads_to) and recommends one; it is answered once (with the
  text the option asks for) or withdrawn once, never edited or deleted (triggers).
- `stage` skips tickets named in nix-fleet backlogs or nix-fleet herdr workspace labels.
- A verdict is dispatched at most once (a blocked card needs the ticket to change first, or your "retry" on
  the block, whose guidance becomes a note on the next draft); `valid` verdicts expire
  after 7 days. Tickets in the team's review state (Ready for QA) are out of scope: they wait on a human.
- The reconcile gate asks about an executing dispatch whose executor pane is gone or with no card activity for
  `executor.stuck_hours` (not again for that long after "wait"; withdrawn once it clears).
