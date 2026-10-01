# factory: software factory control plane

Linear tickets in niko's domains are checked against code and data, grouped into reviewed dispatches (cohorts of related tickets), executed by
factory-fleet, and written back to Linear. `~/.hermes/factory.db` is the only authoritative tracker.

```
ingest (cron) -> Strategy (groom briefs) -> publish brief -> prune verdicts (cron) -> draft -> plan (cron agent)
  -> review: your notes -> approve -> handoff -> execute (factory-fleet) -> reconcile (cron) -> archive
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

- Dashboard: Hermes dashboard, **Factory** tab, phone first, eight lifecycle workspaces. Only the selected
  workspace is mounted; its decisions, plans and actions stay inside it. The shared header has health,
  refresh and a compact **Needs you** menu. Each menu item opens its owning stage and focuses the actual
  decision or plan question; an executor question belongs in Run, never Tickets or Review.
  `factory.db` changes refresh data over `/stream` without polling, changing tabs or opening panels.
  Stage-local decision cards show options, consequences and ★. Executor answers and options that start or stop
  work or write Linear require a second tap; some require a reason. Lightweight ★ accepts a right swipe;
  left postpones. Canceled gestures do nothing. Refused choices return with their error.
  Long explanations and completed why threads start folded; questions, deadlines, pending work and errors stay visible.
  `why?` asks the planner inline. **Review** contains the plan configurator (`/factory?stage=review&run=<run_id>`;
  legacy `?view=review&run=<run_id>` still works). On a phone it is one column: a sticky **result**
  (the dispatch's predicted result
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
  in the shared header. The ticket button opens each ticket's verdict and evidence. On a desktop a railway map
  sits beside it (now → question → each option on its own track, one row apart → rejoin → … → result; ★ solid, picked path lit, the rest
  faded), one selection with the switches. Open decisions whose ★ starts, stops or writes nothing are the
  stage-local **Quick** lane: tap one to see its options, or "Take all ★" (`POST /decisions/ok`, the chat's "ok").
  Lifecycle tabs have stable `?stage=<stage>` URLs; `run`, `decision` and `ticket` target a specific item.
  Browser Back/Forward restores location; each workspace retains its ticket filters, selection and scroll.
  A selected dispatch that moves stage on refresh is replaced by a link to its new stage, not another dispatch.
  Arrow keys move tab focus; Enter/Space selects. Learn and Costs sit apart from the eight lifecycle stages:
  - **Tickets:** complete ticket ledger (`factory tickets --all`), ownership, scope and ingest status; no draft builder.
    Search/filter and a row's sheet expose verdict evidence and the full audit timeline (`factory ticket-timeline <ID>`).
    Legacy `/factory?ticket=<ID>` still opens the sheet. Ledger rows can link to their current lifecycle stage.
  - **Verify:** only tickets whose backend phase is Verify, with verification-job status and Linear-answer filter.
  - **Draft:** verified eligible tickets, recommended cohort/checkbox builder, unoffered drafts and blocked-ticket retry decisions.
    A pending or failed draft submission survives leaving the workspace; a late success never steals navigation.
  - **Plan:** drafts actually offered to the planner, replans and refused plan publications. `planning_requested_at`
    records an offer, not proof an agent is running; `planning_error` records publication refusal, even without an offer.
    Old unplanned drafts remain Draft until an actual offer. Planner-job health is labeled separately.
  - **Review:** published draft plans, questions, configurator and approval/Hold/rejection. Held plans remain here;
    replan returns to Plan, retaining notes and answered questions.
  - **Run:** staged/executing dispatches, executor questions and undelivered answers, last activity/blocker/next step,
    and the read-only execution plan.
  - **Reconcile:** done/reconciled dispatches and unresolved pending/sent/failed/held Linear writes, including
    sweep/followup writes without a dispatch. Viewing writes never applies them.
  - **Archive:** all archived dispatches, including rejected drafts, with audit history and predicted versus landed results.
    Lazy `GET /archive` uses `factory status --archived`, not the overview's last-five history.
  - **Learn:** proposed learning approvals, learned evidence and Done for you. **Costs:** throughput and cost ledger.
  Counts name their units: Tickets and Verify count tickets; Draft through Archive count dispatches.
  Draft separately labels ready tickets. Backend records determine phases, not whether a UI panel or plan is open.
  Dispatch rows fold into cards on a phone. UI source is React JSX in `hermes/plugins/factory/dashboard/src/`:
  `index.jsx` owns workspaces, `plan.jsx` the configurator, `railway.jsx` its desktop map, `tickets.jsx` ticket views,
  `why.jsx` explanation threads and `learn.jsx` learnings. React/components come from the dashboard SDK;
  `./install.sh` bundles with `bun build` into gitignored `dist/` and copies the plugin.
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
  in Run after refresh, linked from Needs you, with **Resend recorded answer** (`factory decide resend <id>`, dashboard
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
  existing consequence text. Each kept rule is its own question in the same call (which option it names, or none;
  the question quotes the complete approved rule, including trailing exceptions), so equivalent rules never split one
  confidence: a rule is cited only when the provider's confidence in its own answer is >= 0.85 and it is still
  eligible after the call; sure rules naming different options cite none, agreeing ones cite the most confident
  (ties: the oldest). Guidance only: Jev never answers, voids or changes a decision; the UI never changes
  the selected option from it. Disabled guidance leaves normal review unchanged; a failed call shows an
  unavailable state and a sanitized error (no provider body). A plan question Jev is sure (>= 0.85) asks for pure
  missing investigation is refused at plan time with a StageError telling the planner to check the code and
  decide it in the plan, before anything is written. `investigate` means facts the code or data settle, not
  authority or consent; uncertain judgments preserve human review. The state contains the decision's repos,
  options and evidence notes plus the 8 newest same-repo
  rules a person kept (each with its scope), all quoted as data to judge, never instructions. Judgments are
  fingerprinted over the actual input plus the current eligible rules: an unchanged success is reused across
  propose ticks, changed inputs re-judge, failed calls retry later. Reads remove a rule claim whose rule expired,
  was rejected or left the repo (with its policy category), a relation together with its group once its
  learning is gone, rewritten or relocated, and only the group once its `learning:<root>` is no longer an active or
  proposed learning of the repo (a learning decision's scope is its learning's repo). The propose tick
  refreshes before notify with a 15 s soft budget (socket timeouts, not a hard wall-clock deadline). Calls use the
  remaining budget, least recently attempted first, so repeated failures never starve the rest.
  `factory jev sync` refreshes the whole queue and proposed learnings' relations on demand, nothing else.
  Reads (status/overview/`decide list`) never call the network. Endpoint
  `https://api.typesafe.ai/v1/systemone`, model `jev-1.13.0` (pinned), key `TYPESAFE_API_KEY` in
  `~/.config/secrets/factory-jev.env` (listed in `secrets.env_files`; `[jev]` in factory.toml). Learnings never
  become rules by earned automation: `learning` decisions are excluded from the earned-auto tier and from sweep,
  and legacy open auto learnings read as digest.
- Learnings (`learn.py`, synced each `factory-propose` tick): one- or two-line facts that save agents tokens, each
  with its source, anchors (repo paths) and expiry. **Code map** (what lives at a path) comes only from observed
  verdict evidence (a cited file with its note), one per repo+path, active at once. Plan step text is never a code
  fact: legacy `step:` code map rows are retired (expired with a reason, kept as audit). **Pitfalls** (a blocked
  card's reason) and **house rules** (a plan option label you chose twice, or a non-★ answer with a note) are
  proposed as decisions and reach agents only once you keep them. An active learning expires when trunk changes or
  deletes an anchor. After harvesting commits, Jev compares each open proposal with at most 40 learnings of its
  repo (same kind first, then newest; a proposed one only if older). Each candidate gets its own four-way Choice
  in one batched call, with its text treated as quoted data. Confidence belongs to that comparison, so several
  agreeing candidates cannot dilute a clear conflict merely because its citation is ambiguous. Only comparisons
  with confidence >= 0.85 count; conflicts take priority and never enter a related group. Otherwise the strongest
  `duplicate` or `supports` comparison forms a `learning:<root>` group, folded as Related learnings in the Quick
  lane. Each proposal still requires its own keep/drop choice; Jev never changes a learning's status.
  Learning refresh has a separate 15 s soft budget, least recently attempted first; explicit `factory jev sync`
  has no pass budget but keeps per-call timeouts. Failed calls remain visible and retry later. Unchanged successes
  reuse their judgments until input, candidate metadata, questions, model or confidence threshold changes;
  cached groups update from current target advice without another model call.
  The prune and planner gates hand agents `learnings`
  (house rules and pitfalls for the repo, code map lines for the ticket's cited paths or named files; at most 12),
  dispatch.md gets **Known pitfalls**, and agents cite `L<id>` in verdict reasons, plans and card comments, which
  counts a use. The Learn tab lists them with their provenance (observed evidence, kept by you, or proposed and
  awaiting you, each with its source), relations and uses, next to the cost per verdict and per plan.
- Backups: `factory-backup` (03:00) writes `~/.hermes/factory/backups/factory-YYYY-MM-DD.db` (newest 14), and every
  schema migration first writes `factory-pre-vN.db`. Same disk: protects against bad writes, not disk loss.
- CLI: `factory status|overview|tickets|ticket-timeline|candidates|stage|strategy|draft|decide|ask|handoff|propose|execute|card|reconcile|archive|recover-launch|metrics|backup|jev`.
  `stage` and approving refresh only the repos involved (parallel fetch); the cron keeps the rest fresh.

## Strategy vs Factory

**Strategy** is a separate supporting workspace (`/factory?stage=strategy`) that owns source grooming and intent;
**Factory** owns verification, implementation planning and execution proof. The eight lifecycle workspaces
(Tickets, Verify, Draft, Plan, Review, Run, Reconcile, Archive) are unchanged. Linear snapshots are source and
provenance plus reconciliation ids, not the runtime instruction source for a brief-backed dispatch.

- A **brief** is an approved, self-contained, versioned work brief: title, outcome, acceptance, scope, exclusions,
  decisions, dependencies, resources, risks, evidence — plus server-captured source snapshots (issue id, repo,
  context, route, verdict id/evidence, trunk anchors). Briefs are groomed from cached snapshots by DeepSeek, edited
  by a human, and published as an exact version. Publishing intent does **not** approve execution, override missing
  evidence, answer questions or mutate Linear.
- **Approval boundary:** a human approves a brief (`factory strategy approve` or the API, actor
  `user:dashboard` / an explicit CLI user); agents can never approve or silence-publish a brief. Approving a brief
  authorizes verification and planning from it, not execution. Dispatching still passes the existing review
  decision; a brief is consumed (frozen to a dispatch) at stage.
- **Immutable versions & amendments:** published briefs are immutable. An amendment creates a new draft revision
  with a parent link and reason. A dispatch already staged on a version is never silently changed; if a captured
  source changed since capture, the brief is flagged **needs-amendment** before any new dispatch — an executing
  dispatch stays pinned to its version and shows the discrepancy rather than being overwritten. Publishing
  (approve) refuses a draft whose source has drifted since capture — amend it first rather than approving stale
  intent. Hold/unhold is an explicit readiness change, never an intent or version change. Duplicate publish/stage
  is refused by SQLite transaction/unique constraints; empty or contradictory required fields and dependency cycles
  are rejected.
- **Bounds:** the proposer prepares up to **3** nonexecuting drafts/staged briefs per pass, in stable approved
  order; execution runs at **max_parallel = 2** by default. Reservations are conservative: per-repo claims
  (`repo:OWNER/NAME`, serial per repo), route exclusivity (`route:home`), pane exclusivity (at most one dispatch
  per lead/pane), hierarchical resource keys, and the `global:*` wildcard, which serializes against everything.
  A blocked first candidate never starves an independent later one.
- **Terminal safety:** completing a card or archiving a dispatch never deletes its pane reservation. Terminal
  dispatches free **capacity** automatically (`launch_active` excludes `done`/`reconciled`/`archived`); the pane
  reservation is released only by `release_safe_terminal`, and only after **positive** proof that the pane is idle
  (`agent_status` idle/done) AND its real supervising home reports no active children and no open decisions. The
  supervising home is the route lead only when its registered metadata pane matches the reserved pane, otherwise
  the captain. A missing, malformed or busy home summary is not proof of idle — release **fails closed**.
- **Unknown / uncertain:** an uncertain launch (a send that may or may not have landed) is never replayed or
  auto-released. It is surfaced to the operator and cleared only through explicit manual recovery
  (`factory recover-launch <run_id> --confirm-unsent --reason …`, whose `--actor` defaults to `user:cli`), which
  re-verifies the pane idle and the supervising home idle before deleting. Never an automatic re-send or
  re-release; a fallback captain cannot reset busy work, and reservations are never TTL-stolen from a live or
  unknown executor.
- **Legacy & migration:** the v20 → v21 migration keeps every operator answer and the historical record (it runs
  against the automatic `factory-pre-v21.db` backup and integrity-checks). Historical legacy dispatches — draft,
  staged, executing, done, reconciled — stay NULL-brief and receive a conservative `global:*` claim held until
  archive, so a legacy executing dispatch still bootstraps its reservation even at `max_parallel = 1` (its own
  backfilled `global:*` claim does not conflict with itself) while a NEW staged dispatch is refused by the same
  admission guards (its claims conflict with the legacy `global:*` holder). This is not an indefinite new-dispatch
  bypass: new dispatches require an approved brief. Execution does not trigger a fresh Linear fetch solely to
  reconstruct a narrative.

Exercised (behavioral smoke, not a full-suite claim): real DeepSeek grooming from cached Backlog intake; immutable
published versions with a stable version URL and full amendment history; intent-only publish; refusal to publish a
source-drifted draft and to stage a brief with an unmapped source; hold/unhold as explicit readiness changes; the
chat `list` folding a full overview (briefs plus the whole source list) into a compact, valid briefs+scheduler
summary; and a refused `hold` paste command carrying its shell-quoted reason.

## Runtime coder model selector

- The supported selector is the `omp` harness flag `--model <provider>/<model>` (fuzzy match; `--provider` is
  legacy). DeepSeek V4 Pro is selected as `--model deepseek/deepseek-v4-pro`.
- The factory-fleet primary (captain) is launched by `fleet/launch-factory-primary.sh`, which passes
  `--model "${FACTORY_PRIMARY_MODEL:-anthropic/claude-opus-5-5}"` (thinking via
  `FACTORY_PRIMARY_THINKING`); set `FACTORY_PRIMARY_MODEL` to override the default.
- The cron agent jobs (planner/prune/reconcile) are created by `install.sh` with
  `--provider "${FACTORY_PROVIDER:-deepseek}" --model "${FACTORY_MODEL:-deepseek-v4-pro}"`.
- Crews and secondmates are spawned by the firstmate toolchain's `bin/fm-spawn.sh` (in each
  `~/.local/share/factory-fleet/homes/*` home, not this repo), which takes concrete `--harness`/`--model`/`--effort`
  axes and never parses natural-language rules. The default crew coder model is pinned in the home's
  `config/crew-dispatch.json` (`"default": {"harness": "omp", "model": "deepseek/deepseek-v4-flash", "effort":
  "high"}`). Resolution order: a per-task/captain `--model <provider>/<id>` wins, then the `crew-dispatch.json`
  profile, then `model=default` (the worker `omp` home default). The model is passed to `omp --model <provider>/<id>`;
  `fm-spawn.sh` validates it against `omp models --json` and refuses one not listed for its provider.
- Prose never selects a model. This implementation's code is DeepSeek; no credentials or providers are changed here.

## Invariants (in code: `factory/schema.sql` triggers + CLI checks)

- A dispatch is born a draft and leaves review only approved (`approved_by` set) or rejected with a reason;
  plan steps and notes are writable only while draft (triggers). Once staged it is immutable (`chflags uchg` +
  sha256). Execution is bounded, not single: `max_parallel` (default 2) caps concurrent dispatches, and repo,
  route, pane and hierarchical resource reservations may serialize further.
- A dispatch's pane reservation is retained through `done`, `reconciled` and `archived`; capacity frees via the
  `launch_active` view. Terminal `sent`/`reserved` reservations are cleared only by `release_safe_terminal` after
  positive pane-idle and supervisor-home-idle proof (a missing/malformed home summary fails closed); an `uncertain`
  launch is cleared only by explicit `recover-launch --confirm-unsent` (human `user:cli` actor + reason). Card and
  archive never delete a reservation.
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
- The reconcile gate still checks for a missing executor while work waits on a person. Inactivity alerts pause
  while an executor question is open or its recorded answer has not arrived; existing quiet alerts are withdrawn,
  never answered on the user's behalf. The `executor.stuck_hours` clock uses the latest card event, dispatch start
  or successful answer delivery, not the choice or a failed send. After "wait", it does not ask again for that long.
  Run shows recorded blockers, not a claim that a missing alert proves executor health.
