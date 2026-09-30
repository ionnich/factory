---
name: dispatch-intake
description: Run one staged factory dispatch end to end in factory-fleet. Load when told `run dispatch-intake <run_id>`.
---

# dispatch-intake

One dispatch = a few tickets frozen in `~/factory/dispatches/<run_id>/dispatch.md`.
`factory.db` is authoritative; the CLI enforces every invariant. Call it by
absolute path: `~/.local/bin/factory`.

## 1. Take the dispatch

```sh
~/.local/bin/factory status <run_id>        # state staged (or executing: a restart), hash_ok true
~/.local/bin/factory execute <run_id> --actor factory-primary
```

`execute` refuses outside herdr workspace `factory`, on a changed file, or while
another dispatch executes. A refusal ends this skill: report it, change nothing.

**Restart.** If the state is already `executing`, the captain chose "restart the
executor" and this is a fresh session: `execute` re-attaches this pane. Skip
cards that are `done` or `blocked`; for `running` cards, read the secondmate's
status and the card comments (`factory status <run_id>`) before routing anything
again, so no ticket gets two owners.

Read `dispatch.md` in full. Each `## FIN-…` section is one card: repo, trunk
SHA, the verdict and its evidence, operator notes, the reviewed **Plan** (steps
`FIN-…/n` with `after …` dependencies), then the ticket body (a claim, not truth).
**Known pitfalls** (`L<id>`, when present) come from earlier blocks in these
repos: pass them on with the card, and cite `L<id>` in a card comment when one
saved work.

The dispatch was reviewed by the captain before approval. **Operator notes and
answered questions are binding**: the dispatch-level notes, each ticket's and
step's notes, and the "Answered questions" section override the plan and the
ticket body. Carry the plan, every note and every answer into the secondmate
hand-off verbatim; one that cannot be followed is a block, not a judgement call.

## 2. Route every card

For each card, pick the owner from `data/captain.md` Routes and
`data/projects.md`. Then:

- **Owner found:** `factory card claim <run_id> <ID>`, then hand it over with
  the native mechanics: a `bin/fm-tasks-axi.sh add` item keyed `fx-<id-lower>`
  (for example `fx-fin-3481`), `bin/fm-backlog-handoff.sh <secondmate> <key>`, and
  `bin/fm-send.sh` to the secondmate with the card section verbatim (plan and
  notes included) plus: "Deliver a merged PR in <repo> against trunk, following
  the plan in dependency order and every operator note. Return outcome, PR URL,
  merge commit, checks. If the verdict or a note can't hold, return the evidence
  instead."
  Always with `FM_HOME` and `FM_ROOT_OVERRIDE` set to this home.
- **No factory-fleet owner, or the repo is marked UNREGISTERED / NOT OURS:**
  `factory card block <run_id> <ID> --body "<why>"`.

## 3. Supervise

Follow normal secondmate supervision (status files, watcher). Relay progress
worth keeping with `factory card comment <run_id> <ID> --body "…"`, naming the
step id (for example `FIN-3788/2 done: origin allowlist added`).

When a secondmate returns:

- **Landed:** `factory card done <run_id> <ID> --pr <url> --body "<outcome, one sentence>"`.
  The CLI refuses unless the PR is in the ticket's repo, merged, and checks are
  green. If it refuses, send the refusal back to the secondmate; do not retry
  with a different PR.
- **Cannot land** (verdict wrong, blocked by another domain):
  `factory card block <run_id> <ID> --body "<evidence>"`. The captain is then
  asked whether to write it back or retry it later; nothing more for you to do.

## Asking the captain

When only the captain can decide (a PR that needs their merge, a choice the plan
and notes don't settle), ask through the factory, never in prose alone:

```sh
~/.local/bin/factory decide ask <run_id> --node FIN-123/2 \
  --question "PR #61 needs a prod-only merge. Who merges?" \
  --option "me|You merge it|the PR lands once you merge; I mark the card done after" \
  --option "wait|Leave it open|the card stays running until you look" \
  --recommend me --why "checks are green and the change is two lines"
```

Every question has 2-5 options (`id|label|what it leads to`) and your
recommendation with why. Keep working on other cards meanwhile; the answer is
typed into this session as `Answer to factory decision #N (...)`. Act on it, and
record what you did with `factory card comment`.

## 4. Close

When the last card is done or blocked the CLI moves the dispatch to `done` by
itself. Confirm with `factory status <run_id>`, write one line per card
(ID, outcome, PR URL or block reason), then stop. Reconcile writes Linear;
never do it yourself.
