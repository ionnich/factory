---
name: factory-plan
description: "Software-factory plan job: shape a draft dispatch (a cohort of related tickets) into a reviewable tree: theme, nested tickets, misfits excluded, steps with dependencies, open questions with options and a recommendation, and a review recommendation; written once with `factory draft plan`."
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

A draft with a `Replan: <reason>` note (in `tree[0].notes`) was sent back by the user: the new plan must address that reason and honor the other notes (`tree[].notes`, `earlier_notes`) and answered questions (`decisions` with `chosen`).

Use only `~/.local/bin/factory` (absolute path), `git -C <mirror>` and read-only
file access. No heredocs, pipes, `$(...)`, `-c` or `-e`: the security scan blocks them.

## Shape the dispatch

A dispatch is a cohort: tickets drafted together because they share a Domain or
repo, so one pass through the code lands several of them with little context
switching. Your job is to shape the cohort into a tree:

1. For each ticket, read the evidence files in `mirror` (targeted ranges or
   grep) and confirm where the change goes.
2. Decide the tree:
   - `root`: a theme title (what the dispatch achieves) and a detail of 1-2
     sentences on why these tickets belong together.
   - Nest a ticket `under` another when it builds on it or is a sub-part of it
     (same file/table/surface, or it only makes sense after the other lands).
   - `depends_on` between tickets or steps when order matters.
   - `exclude` a ticket (with the reason) when it does not fit: different
     surface, not atomic with the rest, or much larger/riskier than the others.
     Keep at least one ticket. A one-ticket dispatch is fine.
3. Per kept ticket write 2-6 steps. Each step is one reviewable action with a
   concrete target: a file, function, table, test, or command. Examples: "Add
   origin allowlist to `app/cors.py:load_origins`", "Test: preflight from
   console origin returns 200", "Open PR against main; CI green". Nested steps
   (`FIN-1/2.1`) only when a step has distinct sub-parts. Shared work goes in
   the first ticket that needs it; later tickets depend on that step.
4. Questions: only when the answer changes what gets built (two viable
   approaches with different code, a scope call) and the code can't settle it.
   Put it on the node it affects, with 2-5 options, what each leads to, and the
   one you recommend with why. At most 2; every question is something a
   person has to read, so decide everything else in the plan itself. An
   unanswered question takes your recommendation when the dispatch is
   approved, so recommend what you would do.
5. Review recommendation on `root`: `approve` (default), `hold` (a person
   should look before it starts, e.g. the verdict looks shaky), or `reject`
   (the cohort should not run as drafted), with why in one sentence. If nobody
   answers during the review window of an automatic draft, the factory takes it.

## Record it (once per draft)

```bash
~/.local/bin/factory draft plan <run_id> --steps '[{"id":"root","title":"Console CORS on the commercial API","detail":"Both tickets change the same origin allowlist.","recommend":"approve","why":"Two small changes on one allowlist, both covered by tests."},{"question":"Allow preview origins too?","on":"FIN-1/1","options":[{"id":"prod","label":"Production console only","leads_to":"previews stay blocked; smallest change"},{"id":"both","label":"Production and preview","leads_to":"previews work; wildcard subdomain to review"}],"recommend":"prod","why":"The ticket names only the production console."},{"id":"FIN-2","under":"FIN-1","detail":"Extends FIN-1 to /v2."},{"id":"FIN-3","exclude":"Different repo surface (billing); not atomic with the CORS change."},{"id":"FIN-1/1","title":"Add X to app/y.py:z","detail":"why/how in one or two sentences","depends_on":[]},{"id":"FIN-2/1","title":"Reuse FIN-1/1 for /v2 routes","detail":"...","depends_on":["FIN-1/1"]}]'
```

Ids: `root`, a ticket id, or a step `<TICKET>/<n>[.<m>]`. Every kept ticket
needs 1-12 steps; titles <= 200 chars, detail <= 2000. If the CLI refuses, fix
what it names and retry; never retry the same payload.

## Rules

- Plain language. The reviewer is the operator, not the executor: say what
  changes and how it will be checked, not a code listing.
- Plan only what the ticket and verdict ask for. No drive-by refactors.
- If the evidence shows the work is already done or the ticket is unclear,
  still write the plan, and make step 1 "Confirm ..." with what to check; the
  reviewer decides.
- Finish with one line: `<run_id>: <theme>; kept <tickets>; dropped <tickets or none>; N steps; N questions; recommend <approve|hold|reject>`.
