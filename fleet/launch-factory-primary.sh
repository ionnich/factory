#!/usr/bin/env bash
# Launch factory-fleet's primary (omp) in the current herdr pane. Run it from a pane in workspace `factory`.
# Pins FM_HOME/FM_ROOT_OVERRIDE to this fleet and clears anything a nix-fleet shell may have exported.
set -euo pipefail
FP="$HOME/.local/share/factory-fleet/homes/factory-primary"
TC="$HOME/.local/share/firstmate-toolchain"
cd "$FP"
exec env -u PI_CODING_AGENT_DIR -u FM_STATE_OVERRIDE -u FM_DATA_OVERRIDE -u FM_PROJECTS_OVERRIDE \
  -u FM_CONFIG_OVERRIDE -u FM_PUBLIC_FOLLOWUP_PRIMARY_HOME \
  PATH="$TC/tooling/bin:$TC/roots/no-mistakes/bin:$TC/roots/treehouse/bin:$HOME/.local/bin:$PATH" \
  FM_HOME="$FP" FM_ROOT_OVERRIDE="$FP" FM_OMP_HARNESS=omp \
  "$HOME/.local/bin/omp" --model "${FACTORY_PRIMARY_MODEL:-anthropic/claude-opus-5-5}" --thinking "${FACTORY_PRIMARY_THINKING:-medium}" "$@"
