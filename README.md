# factory: software factory control plane

Linear tickets in niko's domains are checked against code and data, grouped into reviewed dispatches (cohorts of related tickets), executed by
factory-fleet, and written back to Linear. `~/.hermes/factory.db` is the only authoritative tracker.

```
ingest (cron) -> prune verdicts (cron) -> draft -> plan (cron agent) -> review: your notes -> approve -> handoff
  -> execute (factory-fleet) -> reconcile (cron) -> archive
propose (cron) drafts for `auto` repos, takes ★ on decisions whose time came, and tells you (push / digest)
```

## Install

`./install.sh` (idempotent): venv, `factory` on PATH, Hermes scripts/skills/cron jobs (`factory-prune`,
`factory-reconcile`, `factory-propose` (also syncs Linear), `factory-backup`), the planner bot's files and routine
(`[bot:planner] Plan drafts`, in the `planner` profile's own cron store), the `factory` plugin (dashboard tab +
chat tool), factory-fleet primary files.

One-time, by hand:

- Dashboard tab: root `~/.hermes/config.yaml` `plugins.enabled: [factory]`.
- Chat profile: `hermes profile create factory --clone --no-alias`, then in its `config.yaml`:
  `model.default deepseek-v4-pro`, `model.provider deepseek`, `plugins.enabled: [factory]`,
  `platform_toolsets.cli: [factory]`. Run `./install.sh` again to copy the plugin and `SOUL.md`.
- Planner bot: `hermes profile create planner --clone --no-alias --description "…"`, then
  `hermes -p planner config set model.default deepseek-v4-pro`, `model.provider deepseek`, `plugins.enabled '[]'`,
  and in its `profile.yaml` `ui_meta: {hermes-bots: {title: Planner}}` (makes this a Bot Mode install, which gives
  every Bot Chat `message_agent`). Restart the gateway so it serves the profile, run `./install.sh` again.
- factory-fleet: see `fleet/`. Primary runs in herdr workspace `factory`. `handoff` starts it there with
  `fleet/launch-factory-primary.sh` when the workspace or agent is missing (reboot, crash, closed pane).

## Use

- Dashboard: Hermes dashboard, **Factory** tab, phone first, a stack of cards, live (the tab refreshes the moment
  `factory.db` changes, over `/stream`; no polling). **Needs you** is a deck of
  decisions: the question, the options with what each leads to, the recommended one marked ★ and why. Tap an
  option (a second tap confirms ones that start or stop work or write Linear; some ask for a reason), swipe
  right to take ★, left for later. A draft's planner questions come just before its review, and the review node
  in the flow says how many are still open, so you answer the plan before approving it (approval takes ★ on any
  left). A tap answers at once: the card flies off while the server confirms, and comes back on top with the
  reason if it refuses.
  Below the deck the factory is one tab per lifecycle stage: **Ingest** (tickets: ready / needs answer /
  checking / not for us, with the factory's recommended next group one tap away), **Draft** (assemble and
  revise/edit: rows are drafts, open one to see the plan's DAG — cards down a rail: dispatch → review → tickets →
  steps (branching where the plan branches) → results (PR, Linear writes), each decision hanging off the node
  it's about; a ticket's steps stay folded under it ("▸ 5 steps · 1 open question") until tapped, except where a
  question waits — and `+ note` on draft nodes), **Run** (staged, executing, done),
  and **Learn** (reconciled and the last archived ones, with write-backs in the result nodes; throughput and
  **Done for you** — what the factory answered itself this week — live here too). Dispatches are rows in an
  engineering table (stage, dispatch, tickets, progress, what waits on you, age; the columns fold on a phone).
  A row belongs to one stage for its whole life there and only moves forward. UI source is React JSX in
  `hermes/plugins/factory/dashboard/src/index.jsx` (React and components come from the dashboard SDK);
  `./install.sh` bundles it with `bun build` into the gitignored `dist/` and copies the plugin.
- Chat: `hermes -p factory`, also on the iPhone through Hermex (Bot Mode, factory profile; the web dashboard has
  no Bot Mode yet, so digests and pushes are read and answered there or in the Factory tab). It shows drafts,
  takes notes ("note FIN-3788/2: …") and answers decisions (weighty ones through the Hermes approval prompt);
  "ok" to a digest takes every ★ in it with one confirmation. The **planner** bot (`hermes -p planner`, or its
  Bot Chat in Hermex) wrote the plans and explains them ("why 3 steps for FIN-3661?"); it changes nothing.
  The factory bot hands it "ask the planner …" with `message_agent` and relays the reply.
- Decisions (`factory decide list|choose|ok|ask`): every choice the factory needs from you is a decision with 2+
  options, what each leads to, and one recommended with why. Kinds: a draft's review (approve / hold / reject),
  planner questions (at most 2 per draft), executor questions mid-run (`decide ask`; the answer is typed into its
  pane), a blocked ticket (write back / retry with guidance), a missing or quiet executor (restart / wait / stop),
  a held Linear write (apply anyway / skip / do it yourself).
- Asking less (`decide.py`). Each decision gets a tier when asked. **Auto**: the factory takes ★ on its next pass
  and lists it under "Done for you": nothing to weigh (a code check held a Linear write), the executor's first
  crash in a dispatch (restarted once), or a kind where you took ★ the last 5 times (one override and it asks
  again). **Now**: work is stopped on you (an executor question, a second crash): pushed at once to the factory
  Bot Chat (Hermex), at most `notify.interrupts_per_day` (3) a day, the rest wait for the digest. **Digest**:
  everything else, at `notify.digest` (09:00, 17:00), one line each with what silence does. Silence takes ★ 2h
  (review of a factory draft) or 24h (blocked ticket, held write, quiet executor) after the digest, unless your
  last answer of that kind overrode ★; then it waits for you. A person's draft always waits.
- Review: `factory stage` makes a **draft**. The planner bot's routine (every 10m) writes its plan tree: a theme,
  tickets nested under the ones they build on (misfits dropped with a reason), steps per ticket (`FIN-1/2`,
  nested `FIN-1/2.1`) with `depends_on` edges, questions, and a review recommendation. You add notes to the
  dispatch (`root`), a ticket or a step, and answer questions; approving the review decision renders plan, notes
  and answers into the immutable `dispatch.md`, where they bind the executor (open questions take their
  recommendation). Hold stops the auto-start clock; reject archives the draft as a record, and its tickets are
  not drafted again until their verdict changes. Approval re-checks every ticket: one that changed during review
  voids the draft.
- Autonomous: `factory-propose` (every 10m) drafts a cohort in `auto = true` repos: the top candidate plus those
  sharing its Linear Domain, then its repo (up to `stage.max_tickets`); the planner shapes it into a tree and may
  drop misfits; its review goes in the next digest and follows the rules above.
  **Emergency** (no review window, a one-ticket dispatch): the ticket is Urgent in Linear (a person set it) and its
  verdict evidence touches at most 3 files; ★ is taken at once and you get a push. It also hands off approved
  dispatches, takes ★ where it is due, and prints the push / digest (cron stdout goes to the Bot Chat).
  Stop it with `hermes cron pause factory-propose`.
- Backups: `factory-backup` (03:00) writes `~/.hermes/factory/backups/factory-YYYY-MM-DD.db` (newest 14), and every
  schema migration first writes `factory-pre-vN.db`. Same disk: protects against bad writes, not disk loss.
- CLI: `factory status|overview|tickets|candidates|stage|draft|decide|handoff|propose|execute|card|reconcile|archive|metrics|backup`.
  `stage` and approving refresh only the repos involved (parallel fetch); the cron keeps the rest fresh.

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
  text the option asks for) or withdrawn once, never edited or deleted; its tier is fixed when asked and its clock
  (`notified_at`, `due_at`) is set once while open (triggers). A person's draft, or one they held, is never
  started without them.
- `stage` skips tickets named in nix-fleet backlogs or nix-fleet herdr workspace labels.
- A verdict is dispatched at most once (a blocked card needs the ticket to change first, or your "retry" on
  the block, whose guidance becomes a note on the next draft); `valid` verdicts expire
  after 7 days. Tickets in the team's review state (Ready for QA) are out of scope: they wait on a human.
- The reconcile gate asks about an executing dispatch whose executor pane is gone or with no card activity for
  `executor.stuck_hours` (not again for that long after "wait"; withdrawn once it clears).
