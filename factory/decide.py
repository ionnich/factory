"""Decisions: every choice the factory needs from a person. Each has >= 2 options that say what they lead to and
one recommendation with why. Shape and answer-once live in schema triggers (decision_valid, decision_answer_once).

Asking less: each decision gets a tier when it is asked (see _tier). `sweep` takes the recommendation (★) on the
ones whose time came; `notify` pushes the ones that stop work and puts the rest in a digest twice a day. Silence
takes ★ only for kinds where the user's last answer agreed with it; EARNED_AFTER straight ★ answers and the
factory stops asking that kind."""
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

from . import db, dispatch

# Choices that start real work, stop it, or write Linear: the chat tool asks the human through Hermes's approval
# prompt before sending one of these.
WEIGHTY = {("review", "approve"), ("writeback", "apply"), ("executor-gone", "restart"), ("executor-gone", "stop"),
           ("dispatch-stuck", "restart"), ("dispatch-stuck", "stop")}
OPEN = "chosen IS NULL AND void_reason IS NULL"
AUTO = "factory:auto"
EARNED_AFTER = 5  # straight answers taking ★ before the factory takes it without asking
# Kinds where silence takes ★, and how long after the user was told. Every other kind waits for the user.
SILENT = {"review": timedelta(hours=2), "blocked": timedelta(hours=24), "writeback": timedelta(hours=24),
          "dispatch-stuck": timedelta(hours=24)}


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _human(by: str | None) -> bool:
    """A choice a person made (dashboard, chat, CLI). Not the factory's, and not implicit ones: plan questions
    taken along with an approval, flags resolved before decisions existed."""
    return bool(by) and by.startswith(("user", "agent:factory-chat")) and not by.endswith(
        ("(approved with the recommendation)", "(flag resolution)"))


def streak(conn, kind: str) -> int | None:
    """How many of the user's latest answers of this kind in a row took ★ (counted up to EARNED_AFTER); None when
    they never answered one."""
    n = None
    for chosen, rec, by in conn.execute("SELECT chosen, recommended, chosen_by FROM decision WHERE kind=? AND "
                                        "chosen IS NOT NULL ORDER BY chosen_at DESC, id DESC", (kind,)):
        if not _human(by):
            continue
        if chosen != rec:
            return n or 0
        n = (n or 0) + 1
        if n >= EARNED_AFTER:
            return n
    return n


def _tier(conn, kind: str, run_id: str | None, options: list) -> str:
    """auto: nothing for a person to weigh, or ★ earned. now: work is stopped until the user answers. digest: it
    can wait for the next digest. A person's draft, or one they held, is never started without them."""
    earned = kind != "plan" and (streak(conn, kind) or 0) >= EARNED_AFTER
    if kind == "review":
        d = conn.execute("SELECT drafted_by, emergency, held_reason FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
        own = d is not None and d["drafted_by"] == dispatch.PROPOSE and not d["held_reason"]
        return "auto" if own and (d["emergency"] or earned) else "digest"
    if kind == "writeback" and "apply" not in {o["id"] for o in options}:
        return "auto"  # a code check held it (assignee, ticket changed): skipping is the only real answer
    if kind == "executor-gone":  # the first crash is restarted; a second one is the user's
        again = conn.execute("SELECT 1 FROM decision WHERE run_id=? AND kind='executor-gone'", (run_id,)).fetchone()
        return "now" if again else "auto"
    if earned:
        return "auto"
    return "now" if kind == "ask" else "digest"


def option(id: str, label: str, leads_to: str, note: str | None = None) -> dict:
    """`note`: the option needs text from whoever picks it (the prompt shown)."""
    return {"id": id, "label": label, "leads_to": leads_to, **({"note": note} if note else {})}


def open_(conn, kind: str, question: str, options: list, recommended: str, why: str, created_by: str, *,
          run_id: str | None = None, node_id: str = "root", issue_id: str | None = None, ref: str | None = None,
          detail: dict | None = None) -> int:
    tier, now = _tier(conn, kind, run_id, options), db.now()
    return conn.execute(
        "INSERT INTO decision(run_id, node_id, issue_id, kind, ref, question, options_json, recommended, why, "
        "detail_json, created_at, created_by, tier, due_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, node_id, issue_id, kind, ref, question, json.dumps(options), recommended, why,
         json.dumps(detail or {}), now, created_by, tier, now if tier == "auto" else None)).lastrowid


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
        option("writeback", "Leave it blocked", "no retry; it is not drafted again until the ticket changes "
                                                "(reconcile comments the reason on Linear either way)"),
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
def _silent(conn, d) -> timedelta | None:
    """How long after the user was told silence takes ★; None: it waits for them."""
    if d["tier"] != "digest" or d["kind"] not in SILENT:
        return None
    if d["kind"] == "review":
        r = conn.execute("SELECT drafted_by, held_reason FROM dispatch WHERE run_id=?", (d["run_id"],)).fetchone()
        if r is None or r["drafted_by"] != dispatch.PROPOSE or r["held_reason"]:
            return None  # a person's draft, or one they held
    if streak(conn, d["kind"]) == 0:
        return None  # their last answer of this kind overrode ★: silence isn't consent here
    return SILENT[d["kind"]]


def _deadline(conn, d) -> tuple[str | None, str]:
    """When ★ is taken without the user, and what happens if they stay silent."""
    if d["kind"] == "ask":
        return None, "it waits for your confirmed answer; that work is stopped until you answer"
    if d["tier"] == "auto":
        return d["due_at"], "the factory takes ★ on its next pass (a few minutes)"
    if d["due_at"]:
        return d["due_at"], "the factory takes ★"
    if d["kind"] == "plan":
        return None, "★ is taken when the dispatch is approved"
    if d["tier"] == "now":
        return None, "it waits for you; that work is stopped until you answer"
    if (s := _silent(conn, d)) and not d["notified_at"]:
        return None, (f"delivery is not confirmed; it waits for you until then, then takes ★ "
                      f"{s.total_seconds() / 3600:g}h after confirmed Bot Chat delivery")
    if d["kind"] in SILENT and streak(conn, d["kind"]) == 0:
        return None, "it waits for you (you overrode ★ on this kind last time)"
    return None, "it waits for you"


def _row(conn, r) -> dict:
    d = dict(r)
    d["options"] = [{**o, "weighty": (d["kind"], o["id"]) in WEIGHTY} for o in json.loads(d.pop("options_json"))]
    d["detail"] = json.loads(d.pop("detail_json"))
    d["open"] = d["chosen"] is None and d["void_reason"] is None
    if d["kind"] == "plan":  # the configurator's fields (absent on plans written before v13)
        x = d["detail"]
        d.update(key=x.get("key"), now=x.get("now"), evidence=x.get("evidence", []), depends_on=x.get("depends_on"))
        d["options"] = [{"changes": [], "result": None, "cost": None, "risk": None, **o} for o in d["options"]]
    d["deadline"], d["on_timeout"] = _deadline(conn, r) if d["open"] else (None, None)
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


def _learning(cfg, conn, d, choice, note, actor):
    conn.execute("UPDATE learning SET status=? WHERE id=? AND status='proposed'",
                 ("active" if choice == "keep" else "rejected", int(d["ref"])))
    return {}, None


EFFECTS = {"review": _review, "plan": _plan, "blocked": _blocked, "executor-gone": _executor,
           "dispatch-stuck": _executor, "writeback": _writeback, "ask": _ask, "learning": _learning}


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


def ok(cfg, conn, ids: list[int], actor: str) -> list[dict]:
    """The user's "ok" to a digest or push: take ★ on each (options that ask for words get the ★ reason)."""
    out = []
    for did in ids:
        try:
            d = one(conn, did)
            if d is None:
                raise dispatch.StageError(f"no decision {did}")
            out.append(choose(cfg, conn, did, d["recommended"], actor, note=f"ok to ★: {d['why']}"))
        except dispatch.StageError as e:
            out.append({"decision": did, "error": str(e)})
    return out


# ---- the factory's own answers -----------------------------------------------------------------------------------
def _because(conn, d) -> str:
    if d["tier"] != "auto":
        return "you were silent after it reached you"
    if d["kind"] == "review" and conn.execute("SELECT emergency FROM dispatch WHERE run_id=?", (d["run_id"],)).fetchone()[0]:
        return "emergency: Urgent in Linear and a small change"
    if d["kind"] == "writeback" and "apply" not in {o["id"] for o in d["options"]}:
        return "a code check held it; nothing to weigh"
    if d["kind"] == "executor-gone":
        return "first crash of this dispatch: restarted once"
    return f"earned: you took ★ the last {EARNED_AFTER} times"


def sweep(cfg, conn) -> list[dict]:
    """Take ★ on every open decision whose time came: auto ones, and silent ones past their deadline. A review
    that can no longer start (a ticket moved under the plan) is rejected instead; anything else that no longer
    applies is withdrawn."""
    done = []
    for (did,) in conn.execute(
            f"SELECT id FROM decision WHERE {OPEN} AND kind <> 'ask' AND due_at <= ? AND (tier='auto' OR EXISTS "
            "(SELECT 1 FROM notice n, json_each(n.decision_ids_json) j "
            "WHERE j.value=decision.id AND n.delivered_at IS NOT NULL)) ORDER BY id", (db.now(),)).fetchall():
        d = one(conn, did)
        if not d["open"]:
            continue  # an earlier answer in this pass withdrew it (a rejected draft's questions)
        because = _because(conn, d)
        try:
            res = choose(cfg, conn, did, d["recommended"], f"{AUTO} ({because})", note=f"{because}. {d['why']}")
        except dispatch.StageError as e:
            if d["kind"] == "review" and d["recommended"] == "approve":
                res = choose(cfg, conn, did, "reject", f"{AUTO} (could not start)", note=f"not started: {e}")
            else:
                with db.tx(conn):
                    void(conn, "id=?", (did,), f"★ could not be taken: {e}")
                res = {"voided": str(e)}
        done.append({"id": did, "kind": d["kind"], "question": d["question"], "because": because, **res})
    return done


def done_for_you(conn, since: str) -> list[dict]:
    """What the factory answered on its own since `since`."""
    return [_row(conn, r) for r in conn.execute(
        f"{_SELECT} WHERE d.chosen_by LIKE 'factory:%' AND d.kind <> 'plan' AND d.chosen_at > ? ORDER BY d.id", (since,))]


# ---- telling the user (factory Bot Chat on Hermex) -----------------------------------------------------------------
def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _local(iso: str, now: datetime) -> str:
    t = datetime.fromisoformat(iso).astimezone()
    return f"{t:%H:%M}" if t.date() == now.astimezone().date() else f"{t:%a %H:%M}"


def _slot(now: datetime, times: list[str]) -> str | None:
    """The latest digest time (local 'HH:MM') at or before now, as 'YYYY-MM-DD HH:MM'."""
    local = now.astimezone()
    for day in (local, local - timedelta(days=1)):
        past = [t for t in sorted(times) if day.date() < local.date() or t <= f"{local:%H:%M}"]
        if past:
            return f"{day:%Y-%m-%d} {past[-1]}"
    return None


def _line(conn, d, due: str | None, now: datetime) -> str:
    """One decision in one line: the question, ★ and why, and what silence does."""
    rec = next(o for o in d["options"] if o["id"] == d["recommended"])
    if d["kind"] == "review":
        ids = [r[0] for r in conn.execute("SELECT identifier FROM dispatch_ticket WHERE run_id=? ORDER BY rowid",
                                          (d["run_id"],))]
        qs = conn.execute(f"SELECT count(*) FROM decision WHERE run_id=? AND kind='plan' AND {OPEN}",
                          (d["run_id"],)).fetchone()[0]
        q = f"Start {', '.join(ids)}?" + (f" (+{qs} planner question{'s' * (qs != 1)}, ★ on approval)" if qs else "")
    elif d["kind"] == "ask":
        q = f"Executor asks on {d['node_id']}: {d['question']}"
    else:
        q = d["question"]
    silent = ("waits for you" if not due else
              f"starts {_local(due, now)}" if rec["id"] == "approve" else f"★ at {_local(due, now)}")
    if not due and (window := _silent(conn, d)):
        silent = f"★ {window.total_seconds() / 3600:g}h after confirmed Bot Chat delivery; waits until confirmed"
    return f"#{d['id']} {q} ★ {rec['label']}: {_clip(d['why'], 70)} Silent: {silent}"


def notification_execution(cfg) -> str | None:
    """Bind the exec-based proposal script to its exact Hermes cron execution, never the latest run."""
    root = cfg.db.parent / "cron"
    if not (root / "executions.db").exists() or not (root / "jobs.json").exists():
        return None
    jobs = json.loads((root / "jobs.json").read_text())
    jobs = jobs.get("jobs", []) if isinstance(jobs, dict) else jobs
    ids = {j["id"] for j in jobs if j.get("name") == "factory-propose"
           and j.get("deliver") == "bot-chat:factory" and j.get("script") == "factory-propose.sh"
           and j.get("no_agent") is True}
    with sqlite3.connect((root / "executions.db").resolve().as_uri() + "?mode=ro", uri=True) as receipts:
        runs = receipts.execute("SELECT id, job_id FROM executions WHERE status='running' AND pid=?",
                                (os.getppid(),)).fetchall()
    matches = [r[0] for r in runs if r[1] in ids]
    return matches[0] if len(matches) == 1 else None


def acknowledge_notifications(cfg, conn) -> list[int]:
    """Only a successful, delivered cron receipt starts silence; unknown/queued/failed never do."""
    path = cfg.db.parent / "cron" / "executions.db"
    pending = conn.execute("SELECT * FROM notice WHERE execution_id IS NOT NULL AND delivered_at IS NULL").fetchall()
    if not pending or not path.exists():
        return []
    acknowledged = []
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as receipts, db.tx(conn):
        for n in pending:
            receipt = receipts.execute(
                "SELECT finished_at FROM executions WHERE id=? AND status='completed' AND delivery_outcome='delivered'",
                (n["execution_id"],)).fetchone()
            if not receipt or not receipt[0]:
                continue
            delivered = datetime.fromisoformat(receipt[0])
            if delivered.tzinfo is None or not datetime.fromisoformat(n["at"]) <= delivered <= datetime.now(UTC):
                continue
            for did in json.loads(n["decision_ids_json"]):
                d = one(conn, did)
                if d is None or not d["open"] or d["notified_at"]:
                    continue
                window = _silent(conn, d)
                conn.execute("UPDATE decision SET notified_at=?, due_at=coalesce(due_at,?) WHERE id=?",
                             (_iso(delivered), _iso(delivered + window) if window else None, did))
            conn.execute("UPDATE notice SET delivered_at=? WHERE id=?", (_iso(delivered), n["id"]))
            acknowledged.append(n["id"])
    return acknowledged


READY = "Factory · ready to start"


def notify(cfg, conn, swept: list | None = None, now: datetime | None = None, *,
           execution_id: str | None = None) -> list[str]:
    """Messages for the factory Bot Chat now. A push when work is stopped on the user (at most
    notify.interrupts_per_day a day; the rest wait for the digest) or an emergency started without review; the
    digest at notify.digest times (local). Without a bound cron execution this is a non-consuming preview."""
    n = cfg.raw.get("notify", {})
    now = now or datetime.now(UTC)
    url, msgs = n.get("url", ""), []
    with db.tx(conn):
        midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        pushed = conn.execute("SELECT count(*) FROM notice WHERE kind='push' AND at >= ? AND body NOT LIKE ?",
                              (_iso(midnight), f"{READY}%")).fetchone()[0]
        prepared = {r[0] for r in conn.execute("SELECT value FROM notice, json_each(decision_ids_json)")}
        pushed_ids = []
        push = [f"Emergency: dispatch {s.get('run_id')} started without review ({s['because']})."
                for s in swept or () if s["kind"] == "review" and s["because"].startswith("emergency")
                and s.get("chosen") == "approve"]
        urgent = [d for d in rows(conn) if d["tier"] == "now" and not d["notified_at"] and d["id"] not in prepared]
        if urgent and pushed < n.get("interrupts_per_day", 3):
            push += ["Factory · needs you now", *(_line(conn, d, None, now) for d in urgent),
                     f'Reply "ok" to take ★, or "#{urgent[0]["id"]} <option>". {url}'.rstrip()]
            pushed_ids += [d["id"] for d in urgent]
        # A factory draft's review goes out once its plan is written, not at the next digest: only one dispatch runs
        # at a time, so a review waiting overnight idles the factory. Not counted against interrupts_per_day.
        ready = [d for d in rows(conn) if d["kind"] == "review" and not d["notified_at"]
                 and d["id"] not in prepared and _silent(conn, d)]
        if ready:
            push += [READY, *(_line(conn, d, None, now) for d in ready),
                     f'Reply "ok" to take ★, or "#{ready[0]["id"]} hold: why". {url}'.rstrip()]
            pushed_ids += [d["id"] for d in ready]
        if push:
            if execution_id:
                conn.execute("INSERT INTO notice(kind, body, at, execution_id, decision_ids_json) "
                             "VALUES ('push', ?, ?, ?, ?)",
                             ("\n".join(push), _iso(now), execution_id, json.dumps(pushed_ids)))
            msgs.append("\n".join(push))
        slot = _slot(now, n.get("digest", ["09:00", "17:00"]))
        if slot and not conn.execute("SELECT 1 FROM notice WHERE slot=?", (slot,)).fetchone():
            listed = sorted((d for d in rows(conn) if d["tier"] != "auto" and d["kind"] != "plan"),
                            key=lambda d: (d["tier"] != "now", d["id"]))
            last = conn.execute("SELECT max(at) FROM notice WHERE kind='digest'").fetchone()[0]
            auto = done_for_you(conn, max(filter(None, [last, _iso(now - timedelta(days=1))])))
            lines = []
            if listed or auto:
                urgent_n = sum(d["tier"] == "now" for d in listed)
                lines.append(f"Factory digest · {now.astimezone():%H:%M} · " + (
                    f"{len(listed)} waiting on you" + (f" ({urgent_n} urgent)" if urgent_n else "") if listed
                    else "nothing waiting on you"))
                lines += [_line(conn, d, d["due_at"], now) for d in listed]
                if listed:
                    lines.append(f'Reply "ok" to take every ★, or "#{listed[0]["id"]} <option>", '
                                 f'"#{listed[0]["id"]} hold: why". {url}'.rstrip())
                if auto:
                    lines.append("Done for you: " + "; ".join(
                        f"#{d['id']} {_clip(d['question'], 50)} → {next(o['label'] for o in d['options'] if o['id'] == d['chosen'])}"
                        f" ({d['chosen_by'].removeprefix(AUTO).strip(' ()') or d['chosen_by']})" for d in auto))
            if execution_id:
                conn.execute("INSERT INTO notice(kind, slot, body, at, execution_id, decision_ids_json) "
                             "VALUES ('digest', ?, ?, ?, ?, ?)",
                             (slot, "\n".join(lines), _iso(now), execution_id, json.dumps([d["id"] for d in listed])))
            if lines:
                msgs.append("\n".join(lines))
    return msgs
