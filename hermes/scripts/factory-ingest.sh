#!/usr/bin/env bash
# Hermes no-agent cron: Linear ingest + trunk mirror sync. stdout -> local delivery.
export PATH="$HOME/.local/bin:/etc/profiles/per-user/$USER/bin:/run/current-system/sw/bin:/opt/homebrew/bin:$PATH"  # gateway runs with launchd PATH
exec factory ingest
