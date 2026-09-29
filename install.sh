#!/usr/bin/env bash
# Idempotent install of the factory control plane into this machine.
#   - venv + editable package, `factory` on PATH
#   - config symlink, dispatches symlink
#   - Hermes script shims (Hermes rejects symlinks out of ~/.hermes/scripts), skills
#   - Hermes cron jobs, created only when missing (matched by name)
# Model for agent jobs: FACTORY_MODEL / FACTORY_PROVIDER (default deepseek).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
provider="${FACTORY_PROVIDER:-deepseek}"
model="${FACTORY_MODEL:-deepseek-v4-pro}"

[ -x "$here/.venv/bin/python" ] || uv venv -q "$here/.venv" --python 3.12
uv pip install -q --python "$here/.venv/bin/python" -e "$here"
mkdir -p "$HOME/.local/bin" "$HOME/.config/factory" "$hermes_home/factory/mirrors" "$hermes_home/scripts" "$here/dispatches/_archived"
ln -sfn "$here/.venv/bin/factory" "$HOME/.local/bin/factory"
ln -sfn "$here/factory.toml" "$HOME/.config/factory/factory.toml"
ln -sfn "$here/dispatches" "$hermes_home/factory/dispatches"

for s in "$here"/hermes/scripts/*.sh; do
  install -m 0755 "$s" "$hermes_home/scripts/$(basename "$s")"
done
for d in "$here"/hermes/skills/*/; do
  name="$(basename "$d")"
  mkdir -p "$hermes_home/skills/factory/$name"
  cp -R "$d". "$hermes_home/skills/factory/$name/"
done
# Dashboard bundle: JSX -> one IIFE; React is external (the dashboard's SDK provides it), so no node_modules.
dash="$here/hermes/plugins/factory/dashboard"
(cd "$dash" && bun build src/index.jsx --format iife --outfile dist/index.js >/dev/null)
# Plugin: default profile serves the dashboard tab; the `factory` chat profile gets the tool (profile config:
# plugins.enabled [factory], platform_toolsets.cli [factory], deepseek) — created once by hand, see README.md.
for dest in "$hermes_home" "$hermes_home/profiles/factory"; do
  [ -d "$dest" ] || continue
  rm -rf "$dest/plugins/factory" && mkdir -p "$dest/plugins" && cp -R "$here/hermes/plugins/factory" "$dest/plugins/factory"
done
[ -d "$hermes_home/profiles/factory" ] && cp "$here/hermes/profiles/factory/SOUL.md" "$hermes_home/profiles/factory/SOUL.md"

# factory-fleet primary: local charter + dispatch-intake skill (home data/ and the skill are untracked there)
fp="$HOME/.local/share/factory-fleet/homes/factory-primary"
if [ -d "$fp" ]; then
  cp "$here/fleet/factory-primary/captain.md" "$fp/data/captain.md"
  mkdir -p "$fp/.omp/skills" && rm -rf "$fp/.omp/skills/dispatch-intake"
  cp -R "$here/fleet/factory-primary/dispatch-intake" "$fp/.omp/skills/dispatch-intake"
  cp "$here/fleet/factory-primary/omp-config.yml" "$fp/.omp/config.yml"
fi

boards="$(hermes kanban boards list 2>/dev/null || true)"
grep -q '^ *factory ' <<<"$boards" \
  || hermes kanban boards create factory --name "Software factory" --description "Dispatch cards; factory.db is authoritative"

jobs="$(hermes cron list 2>/dev/null || true)"   # capture first: grep -q on a live pipe + pipefail = SIGPIPE false negative
have_job() { grep -qE "Name: +$1\$" <<<"$jobs"; }

have_job factory-ingest || hermes cron create "every 20m" --no-agent \
  --script factory-ingest.sh --name factory-ingest --deliver local

have_job factory-prune || hermes cron create "every 20m" "$(cat "$here/hermes/prompts/prune.md")" \
  --script factory-prune-gate.sh --skill factory-prune --workdir "$hermes_home/factory/mirrors" \
  --provider "$provider" --model "$model" --reasoning-effort medium \
  --name factory-prune --deliver local

have_job factory-reconcile || hermes cron create "every 20m" "$(cat "$here/hermes/prompts/reconcile.md")" \
  --script factory-reconcile-gate.sh --skill factory-reconcile --workdir "$hermes_home/factory" \
  --provider "$provider" --model "$model" --reasoning-effort low \
  --name factory-reconcile --deliver local

# Nightly consistent copy of factory.db to ~/.hermes/factory/backups (newest 14 kept).
have_job factory-backup || hermes cron create "0 3 * * *" --no-agent \
  --script factory-backup.sh --name factory-backup --deliver local

# Drafts dispatches for `auto` repos, hands off approved ones, takes ★ on decisions whose time came, and prints
# pushes / the twice-daily digest (delivered to the factory Bot Chat, i.e. Hermex). Pause this job to stop it.
have_job factory-propose || hermes cron create "every 10m" --no-agent \
  --script factory-propose.sh --name factory-propose --deliver bot-chat:factory

# Writes the plan tree of each new draft so it can be reviewed.
have_job factory-plan || hermes cron create "every 10m" "$(cat "$here/hermes/prompts/plan.md")" \
  --script factory-plan-gate.sh --skill factory-plan --workdir "$hermes_home/factory/mirrors" \
  --provider "$provider" --model "$model" --reasoning-effort medium \
  --name factory-plan --deliver local

hermes cron list
