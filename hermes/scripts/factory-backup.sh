#!/usr/bin/env bash
# Hermes no-agent cron: factory backup. stdout -> local delivery.
export PATH="$HOME/.local/bin:/etc/profiles/per-user/$USER/bin:/run/current-system/sw/bin:/opt/homebrew/bin:$PATH"  # gateway runs with launchd PATH
exec factory backup
