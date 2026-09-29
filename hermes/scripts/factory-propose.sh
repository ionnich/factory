#!/usr/bin/env bash
# Hermes no-agent cron: factory propose. Prints only what the user needs to hear (push / digest); empty = silent.
export PATH="$HOME/.local/bin:/etc/profiles/per-user/$USER/bin:/run/current-system/sw/bin:/opt/homebrew/bin:$PATH"  # gateway runs with launchd PATH
exec factory propose --announce  # stdout is delivered to bot-chat:factory (Hermex)
