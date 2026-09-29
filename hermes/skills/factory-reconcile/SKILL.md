---
name: factory-reconcile
description: "Software-factory reconcile job: review planned Linear write-backs, polish their prose, downgrade any doubtful write to a flag, then apply with `factory reconcile apply`."
version: 1.0.0
author: nich
platforms: [macos]
metadata:
  hermes:
    tags: [factory, linear, reconcile]
prerequisites:
  commands: [factory]
---

# factory-reconcile

The code decided what may be written to Linear. You make the words readable and
catch anything that looks wrong. You can never add a write, change a state, or
turn a flag back into a write: the CLI and the database refuse all three.

Always call the CLI by absolute path: `~/.local/bin/factory`.

## Input

The prompt starts with the gate's JSON: `context.runs[]`, each with `run_id`
and `writes[]` (`identifier`, `op` = state | comment | description | create,
`decision` = apply | skip | flag, `rule`, `reason`, `payload`, `status`).
A `create` row is a new follow-up ticket split out of `identifier`; its
`payload` has `title` and `description`.

Use only `~/.local/bin/factory`. Never query the database, run scripts, or call
Linear any other way: everything you need is in the gate JSON and
`factory ticket <IDENT>`.

## Per run

1. For each `apply` write, read `factory ticket <IDENT>` if you need context.
2. `comment`, `description` and `create` rows: if the draft is hard to read,
   rewrite it in plain language:

   ```bash
   ~/.local/bin/factory reconcile resolve <run_id> FIN-123 --op comment --body 'Already done on trunk: ...'
   ```

   Keep every URL, commit hash and ticket id from the draft; the CLI refuses a
   rewrite that drops one. A `description` rewrite must start with
   `## Completion` and keep the `Outcome:`, `PR:`, `Commit:`, `Checks:` lines.
   A good draft needs no rewrite.
3. Downgrade a `state` write to a flag (held) when anything in the ticket
   suggests a person is still working on it or disagrees (recent human
   comments, a linked open PR by someone else, a reopened state). The user is
   then asked (apply anyway / skip / do it in Linear); your reason is what they
   read, so write it for them. A row with `approved_by` set was re-applied by
   the user: leave it alone.

   ```bash
   ~/.local/bin/factory reconcile resolve <run_id> FIN-123 --op state --flag 'owner commented yesterday that work continues'
   ```

   Downgrade a `create` row to a flag if the parent ticket already links an
   open ticket for the same ask (it would be a duplicate).

4. `~/.local/bin/factory reconcile apply <run_id>`. It re-checks every gate
   against live Linear, sends state first, then description, then new
   follow-up tickets, then comments, and turns held writes into decisions for
   the user. Exit 1 means some writes
   failed; they retry next run.

Never write to Linear any other way. Finish with one line per run:
`<run_id>: N applied, M flagged, K failed`.
