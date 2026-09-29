"""Decisions: every choice the factory needs from a person. Each has >= 2 options that say what they lead to and
one recommendation with why. A person answers it (or, for a draft's review and plan questions, the recommendation
is taken when time runs out); the answer's effect runs here. Shape and answer-once live in schema triggers
(decision_valid, decision_answer_once)."""
import json
from datetime import UTC, datetime, timedelta

from . import db, dispatch

# Choices that start real work, stop it, or write Linear: the chat tool asks the human through Hermes's approval
# prompt before sending one of these.
WEIGHTY = {("review", "approve"), ("writeback", "apply"), ("executor-gone", "restart"), ("executor-gone", "stop"),
           ("dispatch-stuck", "restart"), ("dispatch-stuck", "stop")}
OPEN = "chosen IS NULL AND void_reason IS NULL"


def option(id: str, label: str, leads_to: str, note: str | None = None) -> dict:
    """`note`: the option needs text from whoever picks it (the prompt shown)."""
    return {"id": id, "label": label, "leads_to": leads_to, **({"note": note} if note else {})}


def open_(conn, kind: str, question: str, options: list, recommended: str, why: str, created_by: str, *,
          run_id: str | None = None, node_id: str = "root", issue_id: str | None = None, ref: str | None = None,
          detail: dict | None = None) -> int:
    return conn.execute(
        "INSERT INTO decision(run_id, node_id, issue_id, kind, ref, question, options_json, recommended, why, "
        "detail_json, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, node_id, issue_id, kind, ref, question, json.dumps(options), recommended, why,
         json.dumps(detail or {}), db.now(), created_by)).lastrowid


def void(conn, where: str, params: tuple, reason: str) -> int:
    """Withdraw open decisions that no longer apply (the draft was rejected, the dispatch finished, ...)."""
    return conn.execute(f"UPDATE decision SET void_reason=?, void_at=? WHERE {OPEN} AND ({where})",
                        (reason, db.now(), *params)).rowcount


# ---- builders: one per place the factory needs a person -------------------------------------------------------
def review_options(held: bool) -> list:
    opts = [option("approve", "Approve & start", "the plan, notes and answers freeze; factory-fleet builds it and "
                                                 "opens PRs; reconcile then writes the results to Linear")]
    if not held:
        opts.append(option("hold", "Hold", "no automatic start; it waits until you approve or reject",
                           note="Why hold it?"))
    opts.append(option("reject", "Reject", "the draft is archived; its tickets are not drafted again until they "
                                           "change", note="Why reject it?"))
    return opts


def review(conn, run_id: str, recommend: str, why: str, created_by: str, held: bool = False) -> int:
    opts = review_options(held)
    if recommend not in {o["id"] for o in opts}:
        recommend = "approve"
    return open_(conn, "review", "Start this dispatch?", opts, recommend, why, created_by, run_id=run_id)


def blocked(conn, run_id: str, ident: str, issue_id: str, reason: str, actor: str) -> int:
    return open_(conn, "blocked", f"{ident} is blocked. What next?", [
        option("writeback", "Write back as blocked", "reconcile comments the reason on the Linear ticket; it is "
                                                     "not drafted again until the ticket changes"),
        option("retry", "Retry in a new dispatch", "the ticket goes back to Ready with your guidance attached as a "
                                                   "note; the next draft plans it again",
               note="What should the retry do differently?"),
    ], "writeback", "Blocks usually need the ticket or its verdict to change first; retrying unchanged tends to "
                    "block again.", actor, run_id=run_id, node_id=ident, issue_id=issue_id,
        detail={"reason": reason})


def executor(conn, run_id: str, kind: str, reason: str, stuck_hours: float) -> int:
    opts = [
        option("restart", "Restart the executor", "factory-primary starts a fresh session in the factory workspace "
                                                  "and resumes this dispatch; finished tickets stay finished"),
        option("wait", "Wait", f"nothing changes; you're asked again after {stuck_hours:g}h if it still holds"),
        option("stop", "Stop the dispatch", "every unfinished ticket is marked blocked; the dispatch closes and "
                                            "reconcile writes back what landed", note="Why stop it?"),
    ]
    if kind == "executor-gone":
        q, rec, why = ("The executor is gone. What now?", "restart",
                       "Nothing moves until an executor runs this dispatch again.")
    else:
        q, rec, why = ("This dispatch has gone quiet. What now?", "wait",
                       "Quiet often means a PR waits on review or CI; a restart drops the executor's working context.")
    return open_(conn, kind, q, opts, rec, why, "factory:watch", run_id=run_id, detail={"reason": reason})


_OP_QUESTION = {
    "state": lambda i, p: f"Move {i} to {p.get('state') or 'its new state'} in Linear?",
    "description": lambda i, p: f"Add the Completion block to {i}?",
    "create": lambda i, p: f"Create the follow-up ticket “{p.get('title', '')}” from {i}?",
    "comment": lambda i, p: f"Post the comment on {i}?",
}


def writeback(conn, run_id: str, issue_id: str, op: str, payload: dict, reason: str) -> int:
    """A write reconcile held back. "Apply anyway" only when the reconcile agent held it; a code gate (assignee,
    ticket changed, already closed) would hold it again."""
    ident = (conn.execute("SELECT identifier FROM linear_latest WHERE issue_id=?", (issue_id,)).fetchone()
             or ["the ticket"])[0]
    opts = [option("skip", "Skip it", "nothing is written to Linear"),
            option("manual", "I'll do it in Linear", "the factory writes nothing; you change the ticket yourself")]
    if (reason or "").startswith("reconcile agent:"):
        opts.insert(0, option("apply", "Apply anyway", "reconcile sends it on its next run; the assignee and "
                                                       "ticket-changed checks still apply"))
    return open_(conn, "writeback", _OP_QUESTION.get(op, _OP_QUESTION["comment"])(ident, payload), opts, "skip",
                 reason or "held by reconcile", "factory:reconcile", run_id=run_id, node_id=ident, issue_id=issue_id,
                 ref=op, detail=payload)


def ask(conn, run_id: str, node: str, question: str, options: list, recommended: str, why: str, actor: str) -> dict:
    """The executor asks mid-run. The answer is sent to its pane; it keeps working on other tickets meanwhile."""
    d = conn.execute("SELECT state FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if d is None or d["state"] != "executing":
        raise dispatch.StageError(f"dispatch {run_id} is {d['state'] if d else 'unknown'}, not executing")
    if node not in {n["id"] for n in dispatch.tree(conn, run_id)}:
        raise dispatch.StageError(f"no node {node!r} in {run_id}; use root, a ticket id or a step id")
    issue = conn.execute("SELECT issue_id FROM dispatch_ticket WHERE run_id=? AND identifier=?",
                         (run_id, node.split("/")[0])).fetchone()
    with db.tx(conn):
        did = open_(conn, "ask", question, options, recommended, why, actor, run_id=run_id, node_id=node,
                    issue_id=issue[0] if issue else None)
    return one(conn, did) or {}


# ---- reading ---------------------------------------------------------------------------------------------------
def _deadline(conn, d) -> tuple[str | None, str]:
    """When the recommendation is taken without you, and what happens if nobody answers."""
    if d["kind"] == "review":
        r = conn.execute("SELECT drafted_by, review_until, held_reason, emergency FROM dispatch WHERE run_id=?",
                         (d["run_id"],)).fetchone()
        if r and r["drafted_by"] == dispatch.PROPOSE and not r["held_reason"]:
            return r["review_until"], "the factory takes the recommendation when the review window ends"
    if d["kind"] == "plan":
        return None, "the recommendation is taken when the dispatch is approved"
    return None, "it waits for you"


def _row(conn, r) -> dict:
    d = dict(r)
    d["options"] = [{**o, "weighty": (d["kind"], o["id"]) in WEIGHTY} for o in json.loads(d.pop("options_json"))]
    d["detail"] = json.loads(d.pop("detail_json"))
    d["open"] = d["chosen"] is None and d["void_reason"] is None
    d["deadline"], d["on_timeout"] = _deadline(conn, r)
    return d


_SELECT = ("SELECT d.*, l.identifier, json_extract(l.raw_json, '$.title') title, json_extract(l.raw_json, '$.url') url "
           "FROM decision d LEFT JOIN linear_latest l USING (issue_id)")


def one(conn, did: int) -> dict | None:
    r = conn.execute(f"{_SELECT} WHERE d.id=?", (did,)).fetchone()
    return _row(conn, r) if r else None


def rows(conn, run_id: str | None = None, open_only: bool = True) -> list:
    where = [f"d.{OPEN.replace(' AND ', ' AND d.')}"] if open_only else []
    params = []
    if run_id:
        where.append("d.run_id=?")
        params.append(run_id)
    sql = f"{_SELECT} {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY d.id"
    return [_row(conn, r) for r in conn.execute(sql, params)]


def open_review(conn, run_id: str) -> dict | None:
    r = conn.execute(f"{_SELECT} WHERE d.run_id=? AND d.kind='review' AND d.{OPEN.replace(' AND ', ' AND d.')}",
                     (run_id,)).fetchone()
    return _row(conn, r) if r else None


# ---- answering -------------------------------------------------------------------------------------------------
def _executing(conn, d):
    s = conn.execute("SELECT state FROM dispatch WHERE run_id=?", (d["run_id"],)).fetchone()
    if s is None or s["state"] != "executing":
        raise dispatch.StageError(f"dispatch {d['run_id']} is no longer executing; this question no longer applies")


def _review(cfg, conn, d, choice, note, actor):
    run_id = d["run_id"]
    dispatch._draft(conn, run_id)
    if choice == "approve":  # open plan questions take their recommendation, then everything freezes together
        conn.execute(f"UPDATE decision SET chosen=recommended, chosen_by=?, chosen_at=? WHERE run_id=? AND "
                     f"kind='plan' AND {OPEN}", (f"{actor} (approved with the recommendation)", db.now(), run_id))
        res = dispatch.approve(cfg, conn, run_id, actor)
        return res, lambda: dispatch.start(cfg, conn, run_id)
    if choice == "hold":
        res = dispatch.hold(conn, run_id, note, actor)
        review(conn, run_id, d["recommended"], d["why"], "factory:hold", held=True)
        return res, None
    void(conn, "run_id=? AND id<>?", (run_id, d["id"]), "the draft was rejected")
    return dispatch.reject(cfg, conn, run_id, note, actor), None


def _plan(cfg, conn, d, choice, note, actor):
    dispatch._draft(conn, d["run_id"])  # answers freeze with the plan at approval
    return {}, None


def _blocked(cfg, conn, d, choice, note, actor):
    return {}, None  # retry: candidates() lets the ticket back in; stage() attaches the guidance


def _executor(cfg, conn, d, choice, note, actor):
    _executing(conn, d)
    run_id = d["run_id"]
    if choice == "restart":
        return {}, lambda: {"resumed": dispatch.resume(cfg, conn, run_id)}
    if choice == "stop":
        def stop():
            left = [r[0] for r in conn.execute("SELECT identifier FROM dispatch_ticket WHERE run_id=? AND "
                                               "card_status IN ('ready','running')", (run_id,))]
            for ident in left:
                dispatch.card(cfg, conn, run_id, ident, "block", actor, body=f"stopped by {actor}: {note}", ask=False)
            return {"stopped": left}
        return {}, stop
    return {}, None  # wait: watch() does not ask again for executor.stuck_hours


def _writeback(cfg, conn, d, choice, note, actor):
    if choice != "apply":
        return {}, None
    n = conn.execute("UPDATE writeback SET decision='apply', status='planned', approved_by=? WHERE run_id=? AND "
                     "issue_id=? AND op=? AND decision='flag'", (actor, d["run_id"], d["issue_id"], d["ref"])).rowcount
    if n != 1:
        raise dispatch.StageError(f"no held {d['ref']} write for {d['identifier']} in {d['run_id']}")
    return {"queued": f"{d['ref']} write for {d['identifier']}; reconcile sends it on its next run"}, None


def _ask(cfg, conn, d, choice, note, actor):
    _executing(conn, d)
    label = next(o["label"] for o in d["options"] if o["id"] == choice)
    text = (f"Answer to factory decision #{d['id']} ({d['question']}): {label}." + (f" Note: {note}" if note else "")
            + f" (by {actor}; also in `factory decide list {d['run_id']} --all`)")
    return {}, lambda: {"sent_to_executor": dispatch.tell_executor(conn, d["run_id"], text)}


EFFECTS = {"review": _review, "plan": _plan, "blocked": _blocked, "executor-gone": _executor,
           "dispatch-stuck": _executor, "writeback": _writeback, "ask": _ask}


def choose(cfg, conn, did: int, choice: str, actor: str, note: str | None = None) -> dict:
    """Answer a decision and run its effect. Database effects commit with the answer (all or nothing); slow side
    effects (starting the executor, telling it the answer) run after, and report their own errors."""
    d = one(conn, did)
    if d is None:
        raise dispatch.StageError(f"no decision {did}")
    if not d["open"]:
        raise dispatch.StageError(f"decision {did} is already " + (f"answered ({d['chosen']} by {d['chosen_by']})"
                                  if d["chosen"] else f"withdrawn ({d['void_reason']})"))
    opt = next((o for o in d["options"] if o["id"] == choice), None)
    if opt is None:
        raise dispatch.StageError(f"decision {did}: choose one of {', '.join(o['id'] for o in d['options'])}")
    note = (note or "").strip() or None
    if opt.get("note") and not note:
        raise dispatch.StageError(f"{opt['label']}: {opt['note']}")
    with db.tx(conn):  # answer first: if the effect fails, both roll back
        conn.execute("UPDATE decision SET chosen=?, chosen_by=?, chosen_at=?, chosen_note=? WHERE id=?",
                     (choice, actor, db.now(), note, did))
        res, after = EFFECTS[d["kind"]](cfg, conn, d, choice, note, actor)
    out = {"decision": did, "chosen": choice, "label": opt["label"], "leads_to": opt["leads_to"], "by": actor,
           **(res or {})}
    if after:
        try:
            out.update(after() or {})
        except dispatch.StageError as e:  # the answer stands; the side effect is retried or shown to the user
            out["after_error"] = str(e)
    return out


def snoozed(conn, run_id: str, kind: str, hours: float) -> bool:
    since = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    return conn.execute("SELECT 1 FROM decision WHERE run_id=? AND kind=? AND chosen='wait' AND chosen_at >= ?",
                        (run_id, kind, since)).fetchone() is not None
