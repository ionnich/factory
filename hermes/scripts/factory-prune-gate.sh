#!/usr/bin/env bash
# Hermes pre-check for factory-prune: last stdout line is the wakeAgent gate JSON.
export PATH="$HOME/.local/bin:/etc/profiles/per-user/$USER/bin:/run/current-system/sw/bin:/opt/homebrew/bin:$PATH"  # gateway runs with launchd PATH
exec factory prune-gate
