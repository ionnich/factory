---
name: dispatch-intake
description: Run one staged factory dispatch in your domain end to end. Load when the factory types `run dispatch-intake <run_id>` into this pane.
---

# dispatch-intake (domain lead)

The factory handed this dispatch straight to you: its tickets are in your domain (the file's
`## Runs in` names you). No captain relays for you. `factory.db` is authoritative and the CLI
enforces every invariant. Call it by absolute path: `~/.local/bin/factory`. Your id is this
home's secondmate id (`.fm-secondmate-home`), used as `--actor` below.

## 1. Take the dispatch

```sh
~/.local/bin/factory status <run_id>                   # state staged (or executing: a restart), hash_ok true
~/.local/bin/factory execute <run_id> --actor <your id>
```

`execute` refuses unless it runs in this pane and the file is intact, and it
refuses while a reservation it needs (repo, route, pane, or a
hierarchical/`global:*` resource) is held by another live run.
A refusal ends this skill: say why in one line, change nothing.

**Restart.** If the state is already `executing`, you were restarted on it: `execute` re-attaches
this pane. Skip cards that are `done` or `blocked`; for `running` cards, check your crews and the
card comments (`factory status <run_id>`) before starting anything again, so no ticket gets two
crews.

Read `~/factory/dispatches/<run_id>/dispatch.md` in full. Each `## FIN-…` section is one card:
repo, trunk SHA, the verdict and its evidence, operator notes, the reviewed **Plan** (steps
`FIN-…/n`, `after …` dependencies), then the ticket body (a claim, not truth). **Operator notes
and answered questions are binding**: they override the plan and the ticket body. Carry the plan,
every note and every answer into each crew brief verbatim; one that cannot be followed is a block,
not a judgement call.

**Brief-backed runs.** A dispatch staged from an approved Strategy brief carries the compiled brief
intent (outcome, acceptance, scope, exclusions, decisions, dependencies, resources, risks, evidence)
and captured source provenance, not the raw Linear narrative. Work from the brief and the plan; do
not re-read the source ticket to reconstruct intent. If the brief conflicts with trunk or its sources
changed since capture (needs-amendment), block the card and tell the captain — never guess a new
intent or silently adopt a changed version. Several dispatches may run at once (bounded by
`max_parallel`, default 2, and by reservations); each `run_id` has its own state, launch reservation
and resource claims, and you never touch another run's pane or reservations.

**Known pitfalls** (`L<id>`, when present) come from earlier blocks in these repos: put them in
each crew brief, and cite `L<id>` in a card comment when one saved work.

## 2. Run every card

For each card:

1. `factory card claim <run_id> <ID> --actor <your id>`.
2. Add it to your own backlog, keyed `fx-<id-lower>` (for example `fx-fin-3481`), with
   `bin/fm-tasks-axi.sh add` in this home, and spawn a crew for it through your normal lifecycle.
   The brief is the card section verbatim (plan and notes included) plus: "Deliver a merged PR in
   <repo> against trunk, following the plan in dependency order and every operator note. Return
   outcome, PR URL, merge commit, checks. If the verdict or a note can't hold, return the evidence
   instead."
3. A card outside your domain, or one no crew can land (cross-domain, wrong repo): `factory card
   block <run_id> <ID> --actor <your id> --body "<why>"`.

## 3. Supervise and report through the factory

Supervise crews as usual. During a dispatch the factory CLI is your channel to the captain, not
the parent status file:

- Progress worth keeping: `factory card comment <run_id> <ID> --actor <your id> --body "FIN-3788/2 done: …"`
  (name the step id).
- **Landed:** `factory card done <run_id> <ID> --actor <your id> --pr <url> --body "<outcome, one sentence>"`.
  The CLI refuses unless the PR is in the ticket's repo, merged, and checks are green. If it
  refuses, send the refusal back to the crew; do not retry with a different PR.
- **Cannot land** (verdict wrong, blocked by another domain): `factory card block … --body "<evidence>"`.
  The captain is then asked whether to write it back or retry later; nothing more for you to do.
- **Only the captain can decide** (a PR that needs their merge, a choice the plan and notes don't
  settle):

  ```sh
  ~/.local/bin/factory decide ask <run_id> --node FIN-123/2 \
    --question "PR #61 needs a prod-only merge. Who merges?" \
    --option "me|You merge it|the PR lands once you merge; I mark the card done after" \
    --option "wait|Leave it open|the card stays running until you look" \
    --recommend me --why "checks are green and the change is two lines"
  ```

  2-5 options (`id|label|what it leads to`) and your recommendation with why. Keep working on
  other cards; the answer is typed into this session as `Answer to factory decision #N (...)`.
  Act on it and record what you did with `factory card comment`.

Never write to Linear: reconcile does that after the dispatch closes.

## 4. Close

When the last card is done or blocked the CLI moves the dispatch to `done` by itself. Confirm
with `factory status <run_id>`, write one line per card (ID, outcome, PR URL or block reason),
then stop and wait for the next hand-off.
