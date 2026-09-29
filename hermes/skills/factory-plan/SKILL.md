---
name: factory-plan
description: "Software-factory plan job: turn a draft dispatch into a reviewable plan tree (steps with dependencies) per ticket, written once with `factory draft plan`."
version: 1.0.0
author: nich
platforms: [macos]
metadata:
  hermes:
    tags: [factory, planning]
prerequisites:
  commands: [factory, git]
---

# factory-plan

A draft dispatch waits for a plan before the user reviews it. You write that plan:
the steps an engineer would take to land each ticket, in dependency order. The
user reads it (often on their phone), leaves notes on steps, then approves. You
never write code, to Linear, to a repo, or to a database.

## Input

The prompt starts with the gate's JSON: `context.draft` with `run_id`, and
`tickets[]` (each: `identifier`, `title`, `repo`, `mirror` = checkout of trunk,
`reason` = why the verdict says the work is needed, `evidence`, `description` =
the ticket body, a claim, not truth).

Use only `~/.local/bin/factory` (absolute path), `git -C <mirror>` and read-only
file access. No heredocs, pipes, `$(...)`, `-c` or `-e`: the security scan blocks them.

## Per ticket

1. Read the evidence files in `mirror` (targeted ranges or grep, at most 6 reads
   per ticket). Confirm where the change goes.
2. Write 2-6 steps. Each step is one reviewable action with a concrete target:
   a file, function, table, test, or command. Examples: "Add origin allowlist to
   `app/cors.py:load_origins`", "Test: preflight from console origin returns 200",
   "Open PR against main; CI green". Use nested steps (`FIN-1/2.1`) only when a
   step has distinct sub-parts.
3. `depends_on` lists steps that must finish first (also across tickets). Leave
   it empty when order doesn't matter. No cycles.

## Record it (once per draft)

```bash
~/.local/bin/factory draft plan <run_id> --steps '[{"id":"FIN-123/1","title":"Add X to app/y.py:z","detail":"why/how in one or two sentences","depends_on":[]},{"id":"FIN-123/2","title":"Test ...","detail":"...","depends_on":["FIN-123/1"]}]'
```

Step ids: `<TICKET>/<n>` or nested `<TICKET>/<n>.<m>`; every ticket needs 1-12
steps; titles <= 200 chars, detail <= 2000. If the CLI refuses, fix what it
names and retry; never retry the same payload.

## Rules

- Plain language. The reviewer is the operator, not the executor: say what
  changes and how it will be checked, not a code listing.
- Plan only what the ticket and verdict ask for. No drive-by refactors.
- If the evidence shows the work is already done or the ticket is unclear,
  still write the plan, and make step 1 "Confirm ..." with what to check; the
  reviewer decides.
- Finish with one line: `<run_id>: N steps for <tickets>`.
