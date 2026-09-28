---
name: dispatch-intake
description: Run one staged factory dispatch end to end in factory-fleet. Load when told `run dispatch-intake <run_id>`.
---

# dispatch-intake

One dispatch = a few tickets frozen in `~/planner/dispatches/<run_id>/dispatch.md`.
`factory.db` is authoritative; the CLI enforces every invariant. Call it by
absolute path: `~/.local/bin/factory`.

## 1. Take the dispatch

```sh
~/.local/bin/factory status <run_id>        # state must be staged, hash_ok true
~/.local/bin/factory execute <run_id> --actor factory-primary
```

`execute` refuses outside herdr workspace `factory`, on a changed file, or while
another dispatch executes. A refusal ends this skill: report it, change nothing.

Read `dispatch.md` in full. Each `## FIN-…` section is one card: repo, trunk
SHA, the verdict and its evidence, then the ticket body (a claim, not truth).

## 2. Route every card

For each card, pick the owner from `data/captain.md` Routes and
`data/projects.md`. Then:

- **Owner found:** `factory card claim <run_id> <ID>`, then hand it over with
  the native mechanics: a `bin/fm-tasks-axi.sh add` item keyed `fx-<id-lower>`
  (for example `fx-fin-3481`), `bin/fm-backlog-handoff.sh <secondmate> <key>`, and
  `bin/fm-send.sh` to the secondmate with the card section verbatim plus:
  "Deliver a merged PR in <repo> against trunk. Return outcome, PR URL, merge
  commit, checks. If the verdict no longer holds, return the evidence instead."
  Always with `FM_HOME` and `FM_ROOT_OVERRIDE` set to this home.
- **No factory-fleet owner, or the repo is marked UNREGISTERED / NOT OURS:**
  `factory card block <run_id> <ID> --body "<why>"`.

## 3. Supervise

Follow normal secondmate supervision (status files, watcher). Relay progress
worth keeping with `factory card comment <run_id> <ID> --body "…"`.

When a secondmate returns:

- **Landed:** `factory card done <run_id> <ID> --pr <url> --body "<outcome, one sentence>"`.
  The CLI refuses unless the PR is in the ticket's repo, merged, and checks are
  green. If it refuses, send the refusal back to the secondmate; do not retry
  with a different PR.
- **Cannot land** (verdict wrong, needs a captain decision, blocked by another
  domain): `factory card block <run_id> <ID> --body "<evidence>"`.

A PR that needs the captain's merge (for example `no-mistakes-prod-only`
repos) stays running until merged; ask the captain once, in plain words, with
the full PR URL.

## 4. Close

When the last card is done or blocked the CLI moves the dispatch to `done` by
itself. Confirm with `factory status <run_id>`, write one line per card
(ID, outcome, PR URL or block reason), then stop. Reconcile writes Linear;
never do it yourself.
