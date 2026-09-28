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
evidence-changed), `context`, `repo`, `mirror` (a checkout of trunk),
`trunk_sha`, and `witnesses` (read-only data sources mapped to this context).

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

- Evidence must be observed in this run. Never cite from memory or from the ticket text alone.
- Do not guess a repo or context. Mapping is config-owned. If the ticket plainly
  belongs elsewhere, use `needs-clarification` and say where it seems to belong.
- One verdict per ticket per run. Skip nothing: an unfinished ticket gets
  `needs-clarification` stating what blocked you.
- Finish with a one-line summary per ticket: `IDENT kind — reason`.
