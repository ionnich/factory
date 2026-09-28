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

hermes kanban boards list 2>/dev/null | grep -q '^ *factory ' \
  || hermes kanban boards create factory --name "Software factory" --description "Dispatch cards; factory.db is authoritative"

have_job() { hermes cron list 2>/dev/null | grep -q -- "$1"; }

have_job factory-ingest || hermes cron create "every 20m" --no-agent \
  --script factory-ingest.sh --name factory-ingest --deliver local

have_job factory-prune || hermes cron create "every 20m" "$(cat "$here/hermes/prompts/prune.md")" \
  --script factory-prune-gate.sh --skill factory-prune --workdir "$hermes_home/factory/mirrors" \
  --provider "$provider" --model "$model" --reasoning-effort medium \
  --name factory-prune --deliver local

hermes cron list
