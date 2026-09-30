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
- factory-fleet: see `fleet/`. Each `[[context]]` in `factory.toml` names its `route`: the factory-fleet home
  (domain lead, `fx-*`) that runs its dispatches, per Domain where one repo has two owners; a ticket with no
  route is never drafted. At approve the dispatch records the one owner of all its tickets (`dispatch.route`,
  also `## Runs in` in dispatch.md). `handoff` sends it straight to that lead's live omp pane
  (`factory-primary/state/<id>.meta`), which runs the `fleet/secondmate/dispatch-intake` skill and reports
  through `factory card` / `decide ask` itself. The captain (primary, herdr workspace `factory`, started with
  `fleet/launch-factory-primary.sh` when missing) gets only dispatches with no single owner or whose lead has no
  live pane: it spawns or wakes the lead and routes as before.

## Use

- Dashboard: Hermes dashboard, **Factory** tab, phone first, a stack of cards, live (the tab refreshes the moment
  `factory.db` changes, over `/stream`; no polling). **Needs you** is a deck of
  decisions: the question, the options with what each leads to, the recommended one marked ★ and why. Tap an
  option (a second tap confirms executor answers and ones that start or stop work or write Linear; some ask for a
  reason), swipe right to take a lightweight ★, left for later. Canceled gestures do nothing. Once confirmed,
  the card flies off while the server responds, and comes back with the reason if refused. `why?` under any decision asks the
  planner inline (see Why? below). A draft's planner questions and its review are one card ("Review FIN-4146 · 2
  questions") that opens the **plan** full-page (the configurator; also `/factory?view=review&run=<run_id>`, so a
  push can link straight in). On a phone it is one column: a sticky **result** (the dispatch's predicted result
  plus one line per question from the picked option, tagged `#id`; the picked path as a breadcrumb — press and
  hold it to mark every difference from ★ — and "vs ★: +1 step · risk"), then the outline: ticket → its result →
  numbered steps ("after 1" chips, files), with operator notes on each node and, while the dispatch is a draft,
  `+ note` on the root, each ticket and each step (the executor reads notes word for word). Each question is a
  switch inside the step it is about (★ marked; each option shows its change, cost, risk); a question that only
  matters under one answer appears once that answer is picked. Flipping a switch sends nothing: the outline redraws
  under that option's plan changes (changed steps get a yellow bar, added ones a `+`, dropped ones strike through
  and fold). The sticky header always shows the review deadline/silence policy and **Hold**, even with unanswered
  questions. Hold requires a reason and submits no plan choices. **Lock in path** answers every open question
  with the selected snapshot; switches, railway choices and Hold are disabled during submission. If a later
  answer fails, earlier saved answers remain and the page refreshes with an explicit partial-completion warning.
  Approval/rejection then appears below; Hold remains in the header. Connection failures and Refresh stay visible
  inside the configurator. The ticket button opens each ticket's verdict and evidence. On a desktop a railway map
  sits beside it (now → question → each option on its own track, one row apart → rejoin → … → result; ★ solid, picked path lit, the rest
  faded), one selection with the switches. Open decisions whose ★ starts, stops or writes nothing are the
  **Quick** lane under the deck: tap one to see its options, or "Take all ★" (`POST /decisions/ok`, the chat's "ok").
  Below the deck the factory is one tab per lifecycle stage: **Tickets** (every ticket in scope or ever touched —
  `factory tickets --all`, fetched only while the tab is open; the overview carries just the per-filter counts —
  newest activity first, filtered ready / needs answer / stale / in dispatch / done / not ours, plus a search; ready
  ones are ticked into a draft, with the factory's recommended next group one tap away; a row opens a sheet with the
  Linear header, the current verdict with its evidence, and the ticket's timeline: Linear updates, every verdict
  (superseded ones too), dispatch transitions, notes, decisions asked and answered, card events, write-backs and our
  own Linear writes, oldest first — `factory ticket-timeline <ID>`; `/factory?ticket=<ID>` opens that sheet),
  **Draft** (rows are drafts; the selected one shows its plan, the configurator above), **Run** (staged, executing,
  done: the plan read-only on the chosen path, card status per ticket, a step ✓ once a card comment says so, e.g.
  "FIN-1/2 done"), and **Learn** (reconciled and the last archived ones: the plan with "predicted" next to "landed"
  — done summary, PR and write-backs per ticket — where untaken answers still flip as ghosts; then **Learnings**,
  **Throughput** with cost, and **Done for you** — what the factory answered itself this week). Dispatches are rows
  in an engineering table (stage, dispatch, tickets, progress, what waits on you, age; the columns fold on a phone).
  A row belongs to one stage for its whole life there and only moves forward. UI source is React JSX in
  `hermes/plugins/factory/dashboard/src/` (`index.jsx` page and deck, `plan.jsx` the plan outline and configurator,
  `railway.jsx` its desktop map, `tickets.jsx` the Tickets tab, `why.jsx` the why threads, `learn.jsx` the
  learnings; React and components come from the dashboard SDK);
  `./install.sh` bundles it with `bun build` into the gitignored `dist/` and copies the plugin.
- Chat: `hermes -p factory`, also on the iPhone through Hermex (Bot Mode, factory profile; the web dashboard has
  no Bot Mode yet, so digests and pushes are read and answered there or in the Factory tab). It shows drafts,
  takes notes ("note FIN-3788/2: …") and answers decisions (weighty ones through the Hermes approval prompt);
  "ok" to a digest takes every ★ in it with one confirmation. The **planner** bot (`hermes -p planner`, or its
  Bot Chat in Hermex) wrote the plans and explains them ("why 3 steps for FIN-3661?"); it changes nothing.
  The factory bot hands it "ask the planner …" with `message_agent` and relays the reply.
- Decisions (`factory decide list|choose|ok|ask|resend`): every choice the factory needs from you is a decision with 2+
  options, what each leads to, and one recommended with why. Kinds: a draft's review (approve / hold / reject),
  planner questions (at most 2 per draft), executor questions mid-run (`decide ask`; the answer is typed into its
  pane), a blocked ticket (write back / retry with guidance), a missing or quiet executor (restart / wait / stop),
  a held Linear write (apply anyway / skip / do it yourself), a proposed learning (keep / drop).
  Executor answers and their exact outbound messages commit together. A failed or interrupted send stays visible
  under Needs you after refresh, with **Resend recorded answer** (`factory decide resend <id>`, dashboard
  `POST /decisions/{id}/resend`, or chat action `resend` with `decision_id`). Resending cannot change the answer;
  it targets the run's current executor pane. Sent answers, terminal runs and concurrent sends are refused.
  Interrupted sends are never replayed automatically: once their sender exits, the UI warns that delivery is
  unknown and a resend may duplicate it. Check the executor first. A reused live PID conservatively blocks recovery.
- Why? (`factory ask new|run|answer|list`): `why?` under any decision in the tab asks the planner inline. `ask new`
  records it (one pending ask per decision) and spawns a detached `factory ask run <id>`, which runs
  `hermes -p planner chat --oneshot -Q -t file --run-budget 150 --query-file <prompt> [--resume <session>]` (the
  decision, plan tree, tickets with verdict and mirror path; explain only, ≤5 lines, cite file:line) and stores
  the answer, or `failed` with the error after 180s or a non-zero exit. Follow-ups resume the decision's last
  session so the planner keeps context. Toolsets: `file` only — Hermes has no read-only terminal toolset, and
  `file` can still write (the prompt forbids it). While a planned draft is in review, "replan with this" under an
  answer (in the plan or on its review) sends the draft back
  (`factory draft replan <run_id> --reason TEXT`, POST /drafts/{run_id}/replan): the reason becomes a binding
  `Replan:` root note, the plan steps are cleared, open review and plan decisions (and their pending asks) are
  withdrawn as `replanned`, and the plan gate picks it up again as unplanned. Notes and answered questions stay.
- Asking less (`decide.py`). Each decision gets a tier when asked. **Auto**: the factory takes ★ on its next pass
  and lists it under "Done for you": nothing to weigh (a code check held a Linear write), the executor's first
  crash in a dispatch (restarted once), or an eligible kind where you took ★ the last 5 times (one override and it asks
  again). Planner and executor questions never earn automatic answers. **Now**: work is stopped on you (an executor
  question, a second crash): pushed at once to the factory Bot Chat (Hermex), at most `notify.interrupts_per_day`
  (3) a day, the rest wait for the digest. **Digest**: everything else, at `notify.digest` (09:00, 17:00).
  Silence takes ★ 2h (review) or 24h (blocked ticket, held write, quiet executor) after **confirmed Bot Chat
  delivery**, unless your last answer of that kind overrode ★. A person's or held draft always waits.
  Preparing output does not start a clock. `propose --announce` binds notices to its exact running
  `factory-propose` cron execution via the exec script's parent PID; the next proposal tick reads
  `~/.hermes/cron/executions.db` read-only. Only `completed` + `delivery_outcome=delivered` starts the window at
  the receipt's finish time. Missing, queued, failed or unknown receipts leave silence disabled; a later delivered
  digest can establish the first receipt. This confirms delivery to Bot Chat, not an iPhone read receipt.
  Manual/unbound proposal output is a non-consuming preview. Schema v18 clears unverified clocks on still-open
  non-auto decisions; historical answers remain unchanged.
- Review: `factory stage` makes a **draft**. The planner bot's routine (every 10m) writes its plan tree: a theme,
  tickets nested under the ones they build on (misfits dropped with a reason), steps per ticket (`FIN-1/2`,
  nested `FIN-1/2.1`) with `depends_on` edges, questions, and a review recommendation. For the phone
  configurator it also predicts outcomes: `result` (one line: what you notice once it lands) on the dispatch and
  each ticket, `files` per step (paths checked at the dispatch trunk in the mirror, like verdict evidence; new
  files `{"path", "new": true}`), and per question a stable `key`, `now` (what the code does today), `evidence`
  (`path:line`), optional `depends_on` (`{question: key, option: id}`: it only matters under that answer), and
  per option the plan `changes` it makes (`{step, becomes: title|null}`, `{add: step}`), its `result`, `cost`,
  `risk`. `factory overview` returns these on tree nodes and plan decisions (plans written before schema v13 have
  them empty). You add notes to the dispatch (`root`), a ticket or a step, and answer questions; approving the
  review decision renders plan, notes and answers (with the chosen option's result and plan changes) into the
  immutable `dispatch.md`, where they bind the executor (open questions take their recommendation; one whose
  `depends_on` answer was not chosen is marked as not applying). Hold stops the auto-start clock; reject archives
  the draft as a record, and its tickets are not drafted again until their verdict changes. Approval re-checks
  every ticket: one that changed during review voids the draft.
- Autonomous: `factory-propose` (every 10m) drafts a cohort in `auto = true` repos: the top candidate plus those
  sharing its Linear Domain, then its repo (up to `stage.max_tickets`); the planner shapes it into a tree and may
  drop misfits; its review goes in the next digest and follows the rules above.
  **Emergency** (no review window, a one-ticket dispatch): the ticket is Urgent in Linear (a person set it) and its
  verdict evidence touches at most 3 files; ★ is taken at once and you get a push. It also hands off approved
  dispatches, takes ★ where it is due, and prints the push / digest (cron stdout goes to the Bot Chat).
  Stop it with `hermes cron pause factory-propose`.
- Jev (`factory/jev.py`): TypeSafe judgment guidance for open decisions, persisted in the `jev_advice` table (one
  replaceable payload per decision, never inside `detail_json` — `decision_answer_once` is untouched) and served
  top-level as `decision.jev`. It classifies each open plan/ask/review decision (`investigate` / `policy` / `human`
  / `unclear` with a confidence), matches an active house rule a person kept (rule id, body and the matching
  option label — never a preselection), and picks a focus (`result`/`changes`/`cost`/`risk`/`none`) from the
  existing consequence text. Guidance only: Jev never answers, voids or changes a decision; the UI never changes
  the selected option from it, and a disabled or failed call leaves the decision as it was, with a visible
  unavailable state and a sanitized error (no provider body). A plan question Jev is sure (>= 0.85) asks for pure
  missing investigation is refused at plan time with a StageError telling the planner to check the code and
  decide it in the plan, before anything is written; authority/consent questions are never treated as mere
  investigation. The state is the decision with its repos, options and evidence notes plus the 8 newest same-repo
  rules a person kept (each with its scope), all quoted as data to judge, never instructions. Judgments are
  fingerprinted over the actual input plus the current eligible rules: an unchanged success is reused across
  propose ticks, changed inputs re-judge, failed calls retry later. Reads remove a rule claim whose rule expired,
  was rejected or left the repo (with its policy category), and a relation together with its group once its
  learning is gone, rewritten or relocated (a learning decision's scope is its learning's repo). The propose tick
  refreshes before notify within 15 s: each call gets at most the time left, and the least recently attempted
  decisions go first, so repeated failures never starve the rest. `factory jev sync` refreshes the whole queue
  and the proposed learnings' relations on demand, nothing else. Reads (status/overview/`decide list`) never
  call the network. Endpoint
  `https://api.typesafe.ai/v1/systemone`, model `jev-1.13.0` (pinned), key `TYPESAFE_API_KEY` in
  `~/.config/secrets/factory-jev.env` (listed in `secrets.env_files`; `[jev]` in factory.toml). Learnings never
  become rules by earned automation: `learning` decisions are excluded from the earned-auto tier and from sweep,
  and legacy open auto learnings read as digest.
- Learnings (`learn.py`, synced each `factory-propose` tick): one- or two-line facts that save agents tokens, each
  with its source, anchors (repo paths) and expiry. **Code map** (what lives at a path) is harvested from verdict
  evidence notes and file names in plan steps, one per repo+path, active at once. **Pitfalls** (a blocked card's
  reason) and **house rules** (a plan option label you chose twice, or a non-★ answer with a note) are proposed as
  decisions and reach agents only once you keep them. An active learning expires when trunk changes or deletes an
  anchor. The prune and planner gates hand agents `learnings` (house rules and pitfalls for the repo, code map
  lines for the ticket's cited paths or named files; at most 12), dispatch.md gets **Known pitfalls**, and agents
  cite `L<id>` in verdict reasons, plans and card comments, which counts a use. The Learn tab lists them with their
  uses next to the cost per verdict and per plan.
- Backups: `factory-backup` (03:00) writes `~/.hermes/factory/backups/factory-YYYY-MM-DD.db` (newest 14), and every
  schema migration first writes `factory-pre-vN.db`. Same disk: protects against bad writes, not disk loss.
- CLI: `factory status|overview|tickets|ticket-timeline|candidates|stage|draft|decide|ask|handoff|propose|execute|card|reconcile|archive|metrics|backup`.
  `stage` and approving refresh only the repos involved (parallel fetch); the cron keeps the rest fresh.

## Invariants (in code: `factory/schema.sql` triggers + CLI checks)

- A dispatch is born a draft and leaves review only approved (`approved_by` set) or rejected with a reason;
  plan steps and notes are writable only while draft (triggers). Once staged it is immutable (`chflags uchg` +
  sha256); at most one executes.
- `execute` only from the dispatch's lead pane or a pane in herdr workspace `factory`; `handoff` resets that
  session (`/new`) first, and refuses while the lead still has crews or decisions open.
- Card `done` needs a merged PR in the ticket's repo with green checks.
- Only `reconcile` writes Linear. State changes need an unassigned-or-lead ticket (and, for verdicts, an unchanged
  `updatedAt`); otherwise comment + held write. The reconcile agent may only reword prose or downgrade apply ->
  flag; a held write is re-applied only by a person (`approved_by`), and then the agent cannot hold it again.
- A decision has >= 2 distinct options (id, label, leads_to) and recommends one; it is answered once (with the
  text the option asks for) or withdrawn once, never edited or deleted; its tier is fixed when asked and its clock
  (`notified_at`, `due_at`) is set once while open (triggers). Silence also requires a confirmed notice receipt;
  executor questions always wait for an explicit answer. A person's draft, or one they held, never starts without them.
- `stage` skips tickets named in nix-fleet backlogs or nix-fleet herdr workspace labels.
- A verdict is dispatched at most once (a blocked card needs the ticket to change first, or your "retry" on
  the block, whose guidance becomes a note on the next draft); `valid` verdicts expire
  after 7 days. Tickets in the team's review state (Ready for QA) are out of scope: they wait on a human.
- The reconcile gate asks about an executing dispatch whose executor pane is gone or with no card activity for
  `executor.stuck_hours` (not again for that long after "wait"; withdrawn once it clears).
