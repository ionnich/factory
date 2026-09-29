"""`factory` tool for the Hermes `factory` chat profile. The only surface that agent gets.

Reads are free. `stage` makes a draft dispatch (a planner adds a plan and a review decision; nothing runs yet).
`note` only touches drafts. `decide` answers a decision (every choice the factory needs from the user: review a
draft, a planner or executor question, a blocked ticket, a stuck executor, a held Linear write). Choices that
start or stop real work or write Linear ask the human through Hermes's approval prompt every time; with no human
channel they are refused and the command to paste is returned instead.
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
        "stage <identifiers> (a cohort of related tickets, up to 8, into a draft dispatch; a planner adds a plan; nothing runs yet); "
        "draft <run_id> (one dispatch: review state, plan tree with node ids, steps, dependencies, notes, its "
        "decisions and Linear writes); note <run_id> <node> <body> (add the user's note to a draft; node ids look "
        "like `root` (whole dispatch), `FIN-3788` (a ticket), `FIN-3788/2` (a step); the executor reads it "
        "verbatim); decisions [run_id] (what waits on the user: each has options with what they lead to, a "
        "recommended option and why); decide <decision_id> <option> [note] (answer one with the user's choice; "
        "some options need a note, e.g. a reason to hold or reject); followup <identifier> <title> <body> [repo] "
        "(queue a new ticket split out of an owned one; reconcile creates it in Linear)."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "tickets", "candidates", "ticket", "stage", "draft",
                                                  "note", "decisions", "decide", "followup"]},
            "run_id": {"type": "string", "description": "dispatch run id for status/draft/note/decisions"},
            "identifier": {"type": "string", "description": "ticket id for `ticket`, e.g. FIN-3481"},
            "identifiers": {"type": "array", "items": {"type": "string"}, "description": "tickets for `stage`"},
            "node": {"type": "string", "description": "plan node id for `note`, from the `draft` tree: `root` (whole "
                                                     "dispatch), `FIN-3788` (a ticket), `FIN-3788/2` or `FIN-3788/2.1` (a step)"},
            "decision_id": {"type": "integer", "description": "decision id for `decide`"},
            "option": {"type": "string", "description": "the option id the user chose, for `decide`"},
            "note": {"type": "string", "description": "text the chosen option asks for (a reason, guidance)"},
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
    if action == "decisions":
        return _run("decide", "list", *([run_id, "--all"] if run_id else []))
    if action == "decide":
        return _decide(params)
    if action == "followup":
        parent, title, body = params.get("identifier"), params.get("title"), params.get("body")
        if not (parent and title and body):
            return '{"ok": false, "error": "identifier (parent), title and body required"}'
        return _run("reconcile", "followup", parent, "--title", title, "--body", body,
                    *(["--repo", params["repo"]] if params.get("repo") else []), "--actor", "agent:factory-chat")
    if action == "note":
        node, body = (params.get("node") or "").strip(), (params.get("body") or "").strip()
        if not (run_id and node and body):
            return '{"ok": false, "error": "run_id, node and body required"}'
        return _run("draft", "note", run_id, "--node", node, f"--body={body}", "--actor", "agent:factory-chat")
    return json.dumps({"ok": False, "error": f"unknown action {action!r}"})


def _decide(params: dict) -> str:
    did, choice = params.get("decision_id"), (params.get("option") or "").strip()
    note = (params.get("note") or "").strip()
    if not did or not choice:
        return '{"ok": false, "error": "decision_id and option required"}'
    listed = json.loads(_run("decide", "list"))
    d = next((x for x in listed if isinstance(x, dict) and x.get("id") == int(did)), None) if isinstance(listed, list) else None
    if d is None:
        return json.dumps({"ok": False, "error": f"no open decision {did}; list them with `decisions`"})
    opt = next((o for o in d["options"] if o["id"] == choice), None)
    if opt is None:
        return json.dumps({"ok": False, "error": f"choose one of {[o['id'] for o in d['options']]}"})
    args = ["decide", "choose", str(int(did)), choice, *([f"--note={note}"] if note else [])]
    if opt.get("weighty"):  # starts/stops real work or writes Linear: a human confirms this one operation
        command = "factory " + " ".join(args)
        why = _approve(command, f"{d['question']} {opt['label']}: {opt['leads_to']}.")
        if why:
            return json.dumps({"ok": False, "error": f"not done: {why}. The user can run it themselves: "
                                                     f"~/.local/bin/{command}"})
        return _run(*args, "--actor", "user:factory-chat")
    return _run(*args, "--actor", "agent:factory-chat (for the user)")


def register(ctx):
    ctx.register_tool(name="factory", toolset="factory", schema=SCHEMA, handler=handle)
