"""`factory` tool for the Hermes `factory` chat profile. The only surface that agent gets.

Reads are free. `stage` freezes tickets into a dispatch (nothing runs yet). `handoff` starts real work on real
repos, so it asks the human through Hermes's approval prompt every time; with no human channel it refuses and
returns the command to paste instead.
"""
import json
import os
import subprocess
from pathlib import Path

FACTORY = str(Path.home() / ".local/bin/factory")
ENV = {**os.environ, "PATH": ":".join([str(Path.home() / ".local/bin"), "/etc/profiles/per-user/nich/bin",
                                       "/run/current-system/sw/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"])}
READS = {"status", "tickets", "candidates", "ticket"}

SCHEMA = {
    "name": "factory",
    "description": (
        "Operate the software factory. Actions: status [run_id] (overview or one dispatch); tickets (owned tickets "
        "with verdicts); candidates (what can be staged, and why the rest cannot); ticket <identifier>; "
        "stage <identifiers> (1-3 tickets into an immutable dispatch; nothing runs yet); handoff <run_id> "
        "(start the dispatch on the executor fleet: real branches and PRs; the user must approve)."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "tickets", "candidates", "ticket", "stage", "handoff"]},
            "run_id": {"type": "string", "description": "dispatch run id for status/handoff"},
            "identifier": {"type": "string", "description": "ticket id for `ticket`, e.g. FIN-3481"},
            "identifiers": {"type": "array", "items": {"type": "string"}, "description": "tickets for `stage`"},
        },
        "required": ["action"],
    },
}


def _run(*args: str) -> str:
    r = subprocess.run([FACTORY, *args], capture_output=True, text=True, timeout=600, env=ENV)
    if r.returncode:
        return json.dumps({"ok": False, "exit": r.returncode, "error": (r.stderr or r.stdout).strip()[-1500:]})
    return r.stdout[-60000:]


def _approve(command: str, description: str) -> str | None:
    """None when a human approved this one operation; otherwise why not. Mirrors Hermes's own one-shot gates."""
    try:
        import tools.approval as approval
        from tools.approval_context import get_current_session_key
        from tools.approval_gateway_wait import _await_gateway_decision
        from tools.approval_prompt import prompt_dangerous_approval
        from tools.terminal_tool import _get_approval_callback
    except Exception as e:  # Hermes internals moved: fail closed, paste-only
        return f"approval prompt unavailable ({type(e).__name__})"
    key = get_current_session_key()
    with approval._lock:
        notify = approval._gateway_notify_cbs.get(key)
    if notify is not None:
        decision = _await_gateway_decision(key, notify, {
            "command": command, "pattern_key": "factory_handoff", "pattern_keys": ["factory_handoff"],
            "description": description, "allow_permanent": False, "allow_session": False}, surface="gateway")
        choice = decision.get("choice") if decision.get("resolved") else "timeout"
    else:
        callback = _get_approval_callback()
        if callback is None:
            return "no human approval channel in this session"
        choice = prompt_dangerous_approval(command, description, allow_permanent=False, allow_session=False,
                                           approval_callback=callback)
    return None if choice in ("once", "session", "always") else f"not approved ({choice})"


def handle(params: dict, **_) -> str:
    action = params.get("action")
    if action in READS:
        arg = params.get("identifier") if action == "ticket" else params.get("run_id") if action == "status" else None
        return _run(action, *([arg] if arg else []))
    if action == "stage":
        ids = [i.strip().upper() for i in params.get("identifiers") or [] if i.strip()]
        return _run("stage", *ids, "--actor", "agent:factory-chat") if ids else '{"ok": false, "error": "no identifiers"}'
    if action == "handoff":
        run_id = (params.get("run_id") or "").strip()
        if not run_id:
            return '{"ok": false, "error": "run_id required"}'
        command = f"factory handoff {run_id}"
        why = _approve(command, f"Start factory dispatch {run_id} on factory-fleet: resets the executor session and "
                                "begins real work (branches, PRs, merges) on the dispatch's repos.")
        if why:
            return json.dumps({"ok": False, "error": f"handoff not started: {why}. "
                               f"The user can run it themselves: ~/.local/bin/{command}"})
        return _run("handoff", run_id)
    return json.dumps({"ok": False, "error": f"unknown action {action!r}"})


def register(ctx):
    ctx.register_tool(name="factory", toolset="factory", schema=SCHEMA, handler=handle)
