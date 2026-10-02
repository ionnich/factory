#!/usr/bin/env bash
# Hermes no-agent cron: factory propose. stdout = push/digest to bot-chat:factory (empty = silent).
# stderr = one-line JSON heartbeat (cron log + ~/.hermes/factory/propose-last.json); not delivered.
export PATH="$HOME/.local/bin:/etc/profiles/per-user/$USER/bin:/run/current-system/sw/bin:/opt/homebrew/bin:$PATH"  # gateway runs with launchd PATH
exec factory propose --announce  # stdout is delivered to bot-chat:factory (Hermex)
