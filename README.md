# factory: software factory control plane

Linear tickets in niko's domains are checked against code and data, frozen into small dispatches, executed by
factory-fleet, and written back to Linear. `~/.hermes/factory.db` is the only authoritative tracker.

```
ingest (cron) -> prune verdicts (cron, DeepSeek) -> stage -> handoff -> execute (factory-fleet) -> reconcile (cron) -> archive
```

## Install

`./install.sh` (idempotent): venv, `factory` on PATH, Hermes scripts/skills/cron jobs (`factory-ingest`,
`factory-prune`, `factory-reconcile`), the `factory` plugin (dashboard tab + chat tool), factory-fleet primary files.

One-time, by hand:

- Dashboard tab: root `~/.hermes/config.yaml` `plugins.enabled: [factory]`.
- Chat profile: `hermes profile create factory --clone --no-alias`, then in its `config.yaml`:
  `model.default deepseek-v4-pro`, `model.provider deepseek`, `plugins.enabled: [factory]`,
  `platform_toolsets.cli: [factory]`. Run `./install.sh` again to copy the plugin and `SOUL.md`.
- factory-fleet: see `fleet/`. Primary runs in herdr workspace `factory`. `handoff` starts it there with
  `fleet/launch-factory-primary.sh` when the workspace or agent is missing (reboot, crash, closed pane).

## Use

- Dashboard: Hermes dashboard, **Factory** tab (read-only).
- Chat: `hermes -p factory`. It can read status, stage, and hand off; handoff raises the Hermes approval prompt.
- CLI: `factory status|tickets|candidates|stage|handoff|execute|card|reconcile|archive` (`--help` on each).

## Invariants (in code: `factory/schema.sql` triggers + CLI checks)

- A dispatch is immutable once staged (`chflags uchg` + sha256); at most one executes.
- `execute` only from a pane in herdr workspace `factory`; `handoff` resets the executor session (`/new`) first.
- Card `done` needs a merged PR in the ticket's repo with green checks.
- Only `reconcile` writes Linear. State changes need an unassigned-or-lead ticket (and, for verdicts, an unchanged
  `updatedAt`); otherwise comment + flag. The reconcile agent may only reword prose or downgrade apply -> flag.
- `stage` skips tickets named in nix-fleet backlogs or nix-fleet herdr workspace labels.
