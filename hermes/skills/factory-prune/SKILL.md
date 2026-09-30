---
name: factory-prune
description: "Software-factory prune job: verify Linear tickets against real code and read-only DB witnesses, then record one evidence-backed verdict per ticket with `factory verdict put`."
version: 1.0.0
author: nich
platforms: [macos]
metadata:
  hermes:
    tags: [factory, linear, verification]
prerequisites:
  commands: [factory, git]
---

# factory-prune

Linear is a claim, not a fact. The code at trunk and the database are the truth.
Your job: for each ticket the gate hands you, decide whether it is actionable
and record exactly one verdict with evidence. You never write to Linear, to a
repo, or to a database.

## Input

The prompt starts with the gate's JSON: `context.tickets[]`, each with
`identifier`, `title`, `why` (new | ticket-changed | context-changed |
evidence-changed | aged), `context`, `repo`, `mirror` (a checkout of trunk),
`trunk_sha`, and `witnesses` (read-only data sources mapped to this context).
A re-check (`why` evidence-changed or aged) also carries `prior` (the current
verdict: kind, target, reason, evidence, its trunk_sha) and `cited_diff` (the
git diff of only the files that verdict cited, prior trunk → current trunk).

## Re-check first (when `prior` is present)

Read `cited_diff` before anything else. The diff is observed in this run.

- It does not touch what `prior.reason` relies on: record `prior.kind` again
  (same target), re-citing the prior `file` evidence (the CLI checks the paths
  still exist at trunk) with notes saying what you checked in the diff. Re-run
  each `sql`/`dagster` witness query the prior cited (data moves without
  commits) and cite the new `witness_log_id`. Nothing else: no ticket read, no
  survey. This should take 1-3 calls.
- It does touch it, or you can't tell from the diff: investigate as below.

## The CLI

Always invoke it by absolute path: `~/.local/bin/factory` (the terminal's login
shell does not have `~/.local/bin` on PATH). Below, `factory` means that path.

## Per ticket

1. `factory ticket <IDENT>`: full ticket text, mapping, and the prior verdict.
2. Investigate read-only:
   - Code: read files under `mirror` (it is exactly `trunk_sha`). Use
     `git -C <mirror> log`/`grep`/`show`. Never modify the mirror.
   - Data: `factory witness <name> "<query>"` for each witness in `witnesses`.
     ClickHouse accepts one SELECT/SHOW/DESCRIBE; Dagster accepts a GraphQL
     query. Each call prints a `witness_log_id`: cite it as evidence. Calls
     are capped at 5 s and 200 rows; keep them narrow.
   - Other tickets: `factory ticket <OTHER>` for suspected duplicates.
3. Choose one kind:
   - `valid`: the ask is still needed and its references exist in code.
     Needs at least one `file` evidence.
   - `already-done`: code and/or data already satisfies the ask.
     Only allowed when the context has witnesses.
   - `stale`: overtaken by events (the surface was removed or redesigned, the ask no longer applies).
   - `duplicate-of`: same ask as another ticket. `--target <OTHER>` plus
     `linear` evidence citing it.
   - `invalid-references`: names a file, table, asset or API that does not
     exist. `--target "<what is missing>"`.
   - `needs-clarification`: you cannot decide from code and data alone.
   Contexts with no witness accept only `valid` or `needs-clarification`.
4. Record it:

   ```bash
   ~/.local/bin/factory verdict put FIN-123 --kind valid --reason "one or two sentences" \
     --evidence '[{"type":"file","path":"finks_dagster/defs/x.py","note":"asset x exists; lacks the retry policy the ticket asks for"},{"type":"dagster","witness_log_id":42,"note":"last 3 runs of x failed with timeout"}]'
   ```

   Pass evidence inline as one single-quoted JSON list. Do not use heredocs,
   pipes or `$(...)`: the terminal's security scan blocks them.

   Evidence types: `file` {path, note} (path must exist at trunk; `sha` defaults to trunk),
   `sql` / `dagster` {witness_log_id, note}, `linear` {ref, note}, `pr` {url, note}.

5. If `factory verdict put` refuses, read the message, fix the evidence or
   choose the kind the rule allows, and retry. Never retry the same payload.

## Rules

- Work one ticket at a time: record its verdict before opening the next ticket.
  Never survey all tickets first; a run cut off mid-batch must keep what it finished.
- Budget: at most 6 investigation calls per ticket. Read targeted line ranges
  or grep, never whole large files. Out of budget → decide on what you have, or
  `needs-clarification` naming the missing fact.
- Evidence must be observed in this run. Never cite from memory or from the ticket text alone.
- Do not guess a repo or context. Mapping is config-owned. If the ticket plainly
  belongs elsewhere, use `needs-clarification` and say where it seems to belong.
- One verdict per ticket per run. Skip nothing: an unfinished ticket gets
  `needs-clarification` stating what blocked you.
- Finish with a one-line summary per ticket: `IDENT kind — reason`.
