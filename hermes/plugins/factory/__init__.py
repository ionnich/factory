"""`factory` tool for the Hermes `factory` chat profile. The only surface that agent gets.

Reads are free. Strategy `list`/`show` read briefs; `groom` (DeepSeek draft) and `amend` (a new draft version)
stay unpublished. `publish` (approve intent) and `hold` are gated through Hermes's approval prompt (once per
call) and act as the human; `stage` passes an approved brief only and never bypasses brief approval.
`note` only touches drafts. `decide` answers a decision (every choice the factory needs from the user: review a
draft, a planner or executor question, a blocked ticket, a stuck executor, a held Linear write); `ok` takes the
recommendation on several (the user's "ok" to a digest). Choices that start or stop real work, write Linear or
direct an executor ask the human through Hermes's approval prompt (once per call); with no human channel they are
refused and the command to paste is returned instead. `resend` sends only an already recorded executor answer.
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
        "list (Strategy briefs and their state/readiness); show <brief_id> (one brief's body, sources and "
        "provenance); groom <identifiers> (DeepSeek drafts a brief from those tickets; a draft only, never "
        "published); amend <brief_id> (draft a new brief version from a body JSON and a reason; never overwrites "
        "the approved version); publish <brief_id> (approve a brief's intent; the human confirms — it does not "
        "approve execution); hold <brief_id> (mark a brief held, with a reason); "
        "stage <brief_id> (stage an approved Strategy brief into a draft dispatch; a planner adds a plan; nothing runs yet); "
        "draft <run_id> (one dispatch: review state, plan tree with node ids, steps, dependencies, notes, its "
        "decisions and Linear writes); note <run_id> <node> <body> (add the user's note to a draft; node ids look "
        "like `root` (whole dispatch), `FIN-3788` (a ticket), `FIN-3788/2` (a step); the executor reads it "
        "verbatim); decisions [run_id] (what waits on the user: each has options with what they lead to, a "
        "recommended option and why); decide <decision_id> <option> [note] (answer one with the user's choice; "
        "some options need a note, e.g. a reason to hold or reject); resend <decision_id> (send only the recorded "
        "executor answer after a delivery failure; status.executor_deliveries shows failures; uncertain delivery "
        "may duplicate, so always confirm with the user); ok <decision_ids> (the user said ok / yes to "
        "a digest or push: take the recommended option on each of those decisions); followup <identifier> <title> "
        "<body> [repo] (queue a new ticket split out of an owned one; reconcile creates it in Linear)."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "tickets", "candidates", "ticket", "list", "show",
                                                  "groom", "amend", "publish", "hold", "stage", "draft", "note",
                                                  "decisions", "decide", "resend", "ok", "followup"]},
            "run_id": {"type": "string", "description": "dispatch run id for status/draft/note/decisions"},
            "brief_id": {"type": "integer", "description": "Strategy brief version id for show/amend/publish/hold/stage"},
            "identifier": {"type": "string", "description": "ticket id for `ticket`, e.g. FIN-3481"},
            "identifiers": {"type": "array", "items": {"type": "string"}, "description": "ticket ids for `groom`"},
            "node": {"type": "string", "description": "plan node id for `note`, from the `draft` tree: `root` (whole "
                                                     "dispatch), `FIN-3788` (a ticket), `FIN-3788/2` or `FIN-3788/2.1` (a step)"},
            "decision_id": {"type": "integer", "description": "decision id for `decide` or `resend`"},
            "decision_ids": {"type": "array", "items": {"type": "integer"},
                             "description": "for `ok`: the #ids of the digest or push the user said ok to"},
            "option": {"type": "string", "description": "the option id the user chose, for `decide`"},
            "note": {"type": "string", "description": "text the chosen option asks for (a reason, guidance)"},
            "reason": {"type": "string", "description": "amendment reason for `amend`; hold reason for `hold`"},
            "title": {"type": "string", "description": "new ticket title, for `followup`"},
            "body": {"type": "string", "description": "note text for `note`; new ticket body (markdown) for `followup`; "
                                                     "JSON brief body for `amend`"},
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
    if action == "list":
        return _run("strategy", "list")
    if action == "show":
        bid = params.get("brief_id")
        return _run("strategy", "show", str(bid)) if bid else '{"ok": false, "error": "brief_id required"}'
    if action == "groom":
        ids = [i.strip().upper() for i in params.get("identifiers") or [] if i.strip()]
        if not ids:
            return '{"ok": false, "error": "no identifiers to groom"}'
        return _run("strategy", "groom", *ids, "--actor", "agent:factory-chat")
    if action == "amend":
        bid = params.get("brief_id")
        body = (params.get("body") or "").strip()
        reason = (params.get("reason") or "").strip()
        if not (bid and body and reason):
            return '{"ok": false, "error": "brief_id, body and reason required"}'
        return _run("strategy", "revise", str(bid), "--body", body, "--reason", reason, "--actor", "agent:factory-chat")
    if action == "publish":
        bid = params.get("brief_id")
        if not bid:
            return '{"ok": false, "error": "brief_id required"}'
        command = f"factory strategy approve {bid}"
        why = _approve(command, f"Publish brief {bid}: approve its intent for verification and planning. "
                                "It does not approve execution, answer questions, or mutate Linear.")
        return _refused(why, command) if why else _run("strategy", "approve", str(bid), "--actor", "user:factory-chat")
    if action == "hold":
        bid = params.get("brief_id")
        reason = (params.get("reason") or "").strip()
        if not (bid and reason):
            return '{"ok": false, "error": "brief_id and reason required"}'
        command = f"factory strategy hold {bid}"
        why = _approve(command, f"Hold brief {bid}: {reason}")
        return _refused(why, command) if why else _run("strategy", "hold", str(bid), "--reason", reason,
                                                       "--actor", "user:factory-chat")
    if action == "stage":
        bid = params.get("brief_id")
        if not bid:
            return '{"ok": false, "error": "brief_id required (stage only an approved brief)"}'
        return _run("stage", "--brief", str(bid), "--actor", "agent:factory-chat")
    if action == "decisions":
        return _run("decide", "list", *([run_id, "--all"] if run_id else []))
    if action == "decide":
        return _decide(params)
    if action == "resend":
        return _resend(params)
    if action == "ok":
        return _ok(params)
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


def _open() -> dict:
    listed = json.loads(_run("decide", "list"))
    return {x["id"]: x for x in listed if isinstance(x, dict)} if isinstance(listed, list) else {}


def _refused(why: str, command: str) -> str:
    return json.dumps({"ok": False, "error": f"not done: {why}. The user can run it themselves: ~/.local/bin/{command}"})


def _ok(params: dict) -> str:
    ids = sorted({int(i) for i in params.get("decision_ids") or []})
    if not ids:
        return '{"ok": false, "error": "decision_ids required"}'
    open_ = _open()
    if missing := [i for i in ids if i not in open_]:
        return json.dumps({"ok": False, "error": f"no open decision {missing}; list them with `decisions`"})
    rec = {i: next(o for o in open_[i]["options"] if o["id"] == open_[i]["recommended"]) for i in ids}
    args = ["decide", "ok", *map(str, ids)]
    weighty = [i for i in ids if rec[i].get("weighty")]
    if weighty:  # one confirmation covers the batch
        command = "factory " + " ".join(args)
        why = _approve(command, "Take the recommendation on: " + "; ".join(
            f"#{i} {open_[i]['question']} {rec[i]['label']}: {rec[i]['leads_to']}" for i in weighty) + ".")
        return _refused(why, command) if why else _run(*args, "--actor", "user:factory-chat")
    return _run(*args, "--actor", "agent:factory-chat (for the user)")


def _decide(params: dict) -> str:
    did, choice = params.get("decision_id"), (params.get("option") or "").strip()
    note = (params.get("note") or "").strip()
    if not did or not choice:
        return '{"ok": false, "error": "decision_id and option required"}'
    d = _open().get(int(did))
    if d is None:
        return json.dumps({"ok": False, "error": f"no open decision {did}; list them with `decisions`"})
    opt = next((o for o in d["options"] if o["id"] == choice), None)
    if opt is None:
        return json.dumps({"ok": False, "error": f"choose one of {[o['id'] for o in d['options']]}"})
    args = ["decide", "choose", str(int(did)), choice, *([f"--note={note}"] if note else [])]
    if opt.get("weighty"):  # real work, Linear writes and executor instructions need a human confirmation
        command = "factory " + " ".join(args)
        why = _approve(command, f"{d['question']} {opt['label']}: {opt['leads_to']}."
                       + (f" Note: {note}" if note else ""))
        if why:
            return _refused(why, command)
        return _run(*args, "--actor", "user:factory-chat")
    return _run(*args, "--actor", "agent:factory-chat (for the user)")


def _resend(params: dict) -> str:
    did = params.get("decision_id")
    if not did:
        return '{"ok": false, "error": "decision_id required"}'
    did = int(did)
    status = json.loads(_run("status"))
    delivery = next((d for d in status.get("executor_deliveries", []) if d["decision_id"] == did), None)
    if delivery is None:
        return json.dumps({"ok": False, "error": f"no unsent executor answer for decision {did}; check status"})
    if delivery["state"] == "sending":
        return json.dumps({"ok": False, "error": f"decision {did} is being sent; concurrent resend refused"})
    command = f"factory decide resend {did}"
    why = _approve(command, f"Resend recorded answer for {delivery['run_id']}: {delivery['question']} "
                   f"{delivery['answer']}. {delivery['error'] or ''} "
                   "The choice will not change. Check the executor first: resending may duplicate delivery.")
    return _refused(why, command) if why else _run("decide", "resend", str(did))


def register(ctx):
    ctx.register_tool(name="factory", toolset="factory", schema=SCHEMA, handler=handle)
