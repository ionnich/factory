"""`factory` tool for the Hermes `factory` chat profile. The only surface that agent gets.

Reads are free. `stage` makes a draft dispatch (a planner adds a plan; nothing runs yet). `note`, `hold` and
`reject` only touch drafts. `approve` freezes a draft and starts real work on real repos, so it asks the human
through Hermes's approval prompt every time; with no human channel it refuses and returns the command to paste
instead.
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
        "stage <identifiers> (1-3 tickets into a draft dispatch; a planner adds a plan; nothing runs yet); "
        "draft <run_id> (one dispatch with its review state and plan tree: node ids, steps, dependencies, notes); "
        "note <run_id> <node> <body> (add the user's note to a draft; node ids look like `root` (whole dispatch), "
        "`FIN-3788` (a ticket), `FIN-3788/2` (a step); the executor reads it verbatim); approve <run_id> (freeze the draft and "
        "start it on the executor fleet: real branches and PRs; the user must approve); hold <run_id> <reason> "
        "(stop a draft's auto-start until approved); reject <run_id> <reason> (discard a draft); "
        "resolve_flag <flag_id> <resolution> (mark a 'needs you' flag handled with what the user decided; "
        "does not change Linear); followup <identifier> <title> <body> [repo] (queue a new ticket split out of an owned one; reconcile creates it in Linear)."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "tickets", "candidates", "ticket", "stage", "draft",
                                                  "note", "approve", "hold", "reject", "resolve_flag", "followup"]},
            "run_id": {"type": "string", "description": "dispatch run id for status/draft/note/approve/hold/reject"},
            "identifier": {"type": "string", "description": "ticket id for `ticket`, e.g. FIN-3481"},
            "identifiers": {"type": "array", "items": {"type": "string"}, "description": "tickets for `stage`"},
            "node": {"type": "string", "description": "plan node id for `note`, from the `draft` tree: `root` (whole "
                                                     "dispatch), `FIN-3788` (a ticket), `FIN-3788/2` or `FIN-3788/2.1` (a step)"},
            "reason": {"type": "string", "description": "the user's reason, for `hold`/`reject`"},
            "flag_id": {"type": "integer", "description": "flag id for `resolve_flag`"},
            "resolution": {"type": "string", "description": "what the user decided, for `resolve_flag`"},
            "title": {"type": "string", "description": "new ticket title, for `followup`"},
            "body": {"type": "string", "description": "note text for `note`; new ticket body (markdown) for `followup`"},
            "repo": {"type": "string", "description": "optional repo the new ticket maps to, for `followup`"},
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
            "command": command, "pattern_key": "factory_approve", "pattern_keys": ["factory_approve"],
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
    run_id = (params.get("run_id") or "").strip()
    if action == "draft":
        action = "status"
        if not run_id:
            return '{"ok": false, "error": "run_id required"}'
    if action in READS:
        arg = params.get("identifier") if action == "ticket" else run_id if action == "status" else None
        return _run(action, *([arg] if arg else []))
    if action == "stage":
        ids = [i.strip().upper() for i in params.get("identifiers") or [] if i.strip()]
        return _run("stage", *ids, "--actor", "agent:factory-chat") if ids else '{"ok": false, "error": "no identifiers"}'
    if action == "resolve_flag":
        if not params.get("flag_id") or not (params.get("resolution") or "").strip():
            return '{"ok": false, "error": "flag_id and resolution required"}'
        return _run("flag", "resolve", str(int(params["flag_id"])), "--resolution", params["resolution"].strip())
    if action == "followup":
        parent, title, body = params.get("identifier"), params.get("title"), params.get("body")
        if not (parent and title and body):
            return '{"ok": false, "error": "identifier (parent), title and body required"}'
        return _run("reconcile", "followup", parent, "--title", title, "--body", body,
                    *(["--repo", params["repo"]] if params.get("repo") else []), "--actor", "agent:factory-chat")
    if action in ("note", "hold", "reject", "approve") and not run_id:
        return '{"ok": false, "error": "run_id required"}'
    if action == "note":
        node, body = (params.get("node") or "").strip(), (params.get("body") or "").strip()
        if not (node and body):
            return '{"ok": false, "error": "node and body required"}'
        return _run("draft", "note", run_id, "--node", node, f"--body={body}", "--actor", "agent:factory-chat")
    if action in ("hold", "reject"):
        reason = (params.get("reason") or "").strip()
        if not reason:
            return '{"ok": false, "error": "reason required"}'
        return _run("draft", action, run_id, f"--reason={reason}", "--actor", "agent:factory-chat")
    if action == "approve":
        command = f"factory draft approve {run_id}"
        why = _approve(command, f"Approve factory dispatch {run_id} and start it on factory-fleet: freezes the plan, "
                                "resets the executor session and begins real work (branches, PRs, merges) on the "
                                "dispatch's repos.")
        if why:
            return json.dumps({"ok": False, "error": f"dispatch not approved: {why}. "
                               f"The user can run it themselves: ~/.local/bin/{command}"})
        return _run("draft", "approve", run_id, "--actor", "user:factory-chat")
    return json.dumps({"ok": False, "error": f"unknown action {action!r}"})


def register(ctx):
    ctx.register_tool(name="factory", toolset="factory", schema=SCHEMA, handler=handle)
