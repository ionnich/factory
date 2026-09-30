"""Reconcile: the only Linear writer. plan -> (agent: resolve prose / downgrade) -> apply.

Rules (code, not prompt):
- Dispatch card done   -> state review_state + Completion block in the description (finks-ddd).
- Dispatch card blocked -> comment with the reason; state untouched (the block's own decision asks what next).
- already-done verdict -> state done_state + comment;   duplicate-of -> canceled_state + comment naming the target.
  State writes only when the verdict is fresh against Linear's current updatedAt AND the ticket is unassigned
  or assigned to linear.lead; otherwise comment + a held write, which becomes a decision for the user.
- needs-clarification / invalid-references / valid / stale: never written.
Every gate is re-checked against a live read immediately before each state write.
"""
import json
import re
import sys
from datetime import UTC, datetime

from . import db, linear, prune
from .config import Config
from . import decide
from .dispatch import StageError, archive

ISSUE_QUERY = "query($id: String!) { issue(id: $id) { %s } }" % linear.ISSUE_FIELDS
STATES_QUERY = "query($id: String!) { team(id: $id) { states { nodes { id name type } } } }"
UPDATE = ("mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) "
          "{ success issue { updatedAt state { name } } } }")
COMMENT = "mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success comment { id } } }"
CREATE = "mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id identifier url } } }"
RELATE = ("mutation($input: IssueRelationCreateInput!) { issueRelationCreate(input: $input) { success } }")
_DOMAIN_LINE = re.compile(r"^\s*\**Domain\**:.*$", re.M)
_COMPLETION = re.compile(r"^## Completion\n.*?(?=^## |\Z)", re.M | re.S)


def team_cfg(cfg: Config, issue: dict) -> dict:
    return cfg.linear.get("team", {}).get(issue["team"]["key"], {})


def state_gate(cfg: Config, issue: dict, expect_updated_at: str | None) -> str | None:
    """Why a state write is NOT allowed now, or None."""
    who = (issue["assignee"] or {}).get("email")
    if who not in (None, cfg.linear["lead"]):
        return f"assigned to {who}"
    if issue["state"]["type"] in ("completed", "canceled"):
        return f"already {issue['state']['name']}"
    if expect_updated_at and issue["updatedAt"] != expect_updated_at:
        return f"ticket changed since the verdict ({expect_updated_at} -> {issue['updatedAt']})"
    return None


def _row(run_id, issue_id, op, payload, decision, rule, reason=None):
    return (run_id, issue_id, op, json.dumps(payload), decision, rule, reason, "planned")


def _dispatch_rows(cfg: Config, conn, run_id: str) -> list:
    d = conn.execute("SELECT state FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if d is None or d["state"] != "done":
        raise StageError(f"dispatch {run_id} is {d['state'] if d else 'unknown'}, not done")
    rows = []
    for t in conn.execute("SELECT * FROM dispatch_ticket WHERE run_id=?", (run_id,)):
        issue = json.loads(prune.latest(conn, t["identifier"])["raw_json"])
        ev = conn.execute("SELECT body, metadata_json FROM card_event WHERE run_id=? AND issue_id=? AND kind IN "
                          "('done','block') ORDER BY id DESC LIMIT 1", (run_id, t["issue_id"])).fetchone()
        if t["card_status"] == "done":
            meta = json.loads(ev["metadata_json"])
            block = (f"## Completion\nOutcome: {ev['body']}\nPR: {meta['pr']}\nCommit: {meta['commit']}\n"
                     f"Checks: {meta['checks']}\n")
            review = team_cfg(cfg, issue)["review_state"]
            why = state_gate(cfg, issue, None)
            rows.append(_row(run_id, t["issue_id"], "description", {"completion": block}, "apply", "card-done"))
            rows.append(_row(run_id, t["issue_id"], "state", {"state": review},
                             "skip" if issue["state"]["name"] == review else "flag" if why else "apply",
                             "card-done", why))
        else:  # blocked
            rows.append(_row(run_id, t["issue_id"], "comment",
                             {"body": f"Factory dispatch {run_id} stopped on this ticket: {ev['body']}"},
                             "apply", "card-blocked"))
    return rows


def _verdict_rows(cfg: Config, conn, run_id: str) -> list:
    rows = []
    for s in prune.owned_in_scope(cfg, conn):
        v = conn.execute("SELECT * FROM verdict WHERE issue_id=? AND superseded_at IS NULL AND written_back_run IS NULL "
                         "AND kind IN ('already-done','duplicate-of') AND id NOT IN (SELECT json_extract(payload_json, "
                         "'$.verdict_id') FROM writeback WHERE status <> 'confirmed' AND json_extract(payload_json, "
                         "'$.verdict_id') IS NOT NULL) AND NOT EXISTS (SELECT 1 FROM decision WHERE kind='writeback' "
                         "AND issue_id=verdict.issue_id AND chosen IS NULL AND void_reason IS NULL)",
                         (s["issue_id"],)).fetchone()
        if v is None:  # none, already written back, pending in an unfinished run, or a held write awaits the user
            continue
        issue = json.loads(s["raw_json"])
        tc = team_cfg(cfg, issue)
        target = tc["done_state"] if v["kind"] == "already-done" else tc["canceled_state"]
        head = "Already done on trunk" if v["kind"] == "already-done" else f"Duplicate of {v['target']}"
        why = state_gate(cfg, issue, v["snapshot_updated_at"])
        evidence = "\n".join(f"- {e.get('path') or e.get('ref') or 'witness #%s' % e.get('witness_log_id')}: "
                             f"{e.get('note', '')}" for e in json.loads(v["evidence_json"]))
        rows.append(_row(run_id, s["issue_id"], "comment",
                         {"body": f"Factory check: {head}. {v['reason']}\n\nEvidence:\n{evidence}",
                          "verdict_id": v["id"]}, "apply", v["kind"]))
        rows.append(_row(run_id, s["issue_id"], "state",
                         {"state": target, "expect_updated_at": v["snapshot_updated_at"], "verdict_id": v["id"]},
                         "flag" if why else "apply", v["kind"], why))
    return rows


def plan(cfg: Config, conn, run_id: str | None) -> dict:
    """Caller ingests first. run_id = a done dispatch; None = sweep closing verdicts."""
    if run_id is None:
        run_id = f"sweep-{datetime.now(UTC):%Y%m%d-%H%M%S}"
        rows = _verdict_rows(cfg, conn, run_id)
    else:
        rows = _dispatch_rows(cfg, conn, run_id)
    with db.tx(conn):  # INSERT OR IGNORE: rows already planned (agent downgrades and prose, a person's apply) stay
        conn.executemany("INSERT OR IGNORE INTO writeback(run_id, issue_id, op, payload_json, decision, rule, reason, "
                         "status) VALUES (?,?,?,?,?,?,?,?)", rows)
    return show(conn, run_id)


def followup(cfg: Config, conn, parent: str, title: str, body: str, repo: str | None, actor: str) -> dict:
    """Queue a new Linear ticket split out of an owned one. Reconcile creates it (same team, parent's Domain
    line, `related` to the parent); nothing is written here."""
    s = prune.latest(conn, parent)
    if not prune.owned(cfg, conn, s):
        raise StageError(f"{parent} is not the factory's concern; no follow-ups from it")
    if not title.strip() or not body.strip():
        raise StageError("follow-up needs a title and a body")
    if conn.execute("SELECT 1 FROM writeback WHERE issue_id=? AND op='create' AND status <> 'failed' "
                    "AND json_extract(payload_json, '$.title')=?", (s["issue_id"], title.strip())).fetchone():
        raise StageError(f"a follow-up titled {title.strip()!r} from {parent} is already queued or created")
    raw = json.loads(s["raw_json"])
    domain = _DOMAIN_LINE.search(raw.get("description") or "")
    head = [domain.group(0).strip()] if domain else []
    head += [f"Repo: {repo}"] if repo else []
    description = "\n\n".join(head + [body.strip(), f"Split out of {parent} ({raw['url']}) by {actor}."])
    run_id = f"followup-{datetime.now(UTC):%Y%m%d-%H%M%S}"
    with db.tx(conn):
        conn.execute("INSERT INTO writeback(run_id, issue_id, op, payload_json, decision, rule, reason, status) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (run_id, s["issue_id"], "create", json.dumps({"title": title.strip(), "description": description,
                                                                     "team_id": raw["team"]["id"],
                                                                     "state": team_cfg(cfg, raw)["todo_state"]}),
                      "apply", "followup", None, "planned"))
    return show(conn, run_id)


def show(conn, run_id: str) -> dict:
    return {"run_id": run_id, "writes": [
        {**dict(r), "payload": json.loads(r["payload_json"])} | {"payload_json": None}
        for r in conn.execute("SELECT w.*, l.identifier FROM writeback w JOIN linear_latest l USING (issue_id) "
                              "WHERE run_id=? ORDER BY l.identifier, op", (run_id,))]}


def resolve(conn, run_id: str, identifier: str, op: str, body: str | None, flag_reason: str | None) -> dict:
    """Agent edits: prose of comment/description rows, or downgrade apply -> flag. Nothing else, and never a write
    a person chose to apply."""
    w = conn.execute("SELECT w.* FROM writeback w JOIN linear_latest l USING (issue_id) "
                     "WHERE w.run_id=? AND l.identifier=? AND w.op=?", (run_id, identifier, op)).fetchone()
    if w is None or w["status"] != "planned":
        raise StageError(f"no planned {op} write for {identifier} in {run_id}")
    payload = json.loads(w["payload_json"])
    with db.tx(conn):
        if body is not None:
            key = {"comment": "body", "description": "completion", "create": "description"}.get(op)
            if key is None:
                raise StageError("only comment and description prose may be edited")
            links = set(re.findall(r"https?://[^\s)>\]]+|\b[0-9a-f]{40}\b|\b[A-Z]+-\d+\b", payload[key]))
            if missing := [x for x in links if x not in body]:
                raise StageError(f"rewrite drops required references: {missing}")
            if op == "description" and not body.startswith("## Completion\n"):
                raise StageError("description rewrite must stay a '## Completion' block")
            conn.execute("UPDATE writeback SET payload_json=? WHERE run_id=? AND issue_id=? AND op=?",
                         (json.dumps({**payload, key: body}), run_id, w["issue_id"], op))
        if flag_reason is not None:  # writeback_no_upgrade trigger also refuses anything but apply -> flag
            if w["approved_by"]:
                raise StageError(f"{w['approved_by']} chose to apply this {op} write; it is not held again")
            if w["decision"] != "apply":
                raise StageError(f"{op} write for {identifier} is already {w['decision']}")
            conn.execute("UPDATE writeback SET decision='flag', reason=? WHERE run_id=? AND issue_id=? AND op=?",
                         (f"reconcile agent: {flag_reason}", run_id, w["issue_id"], op))
    return show(conn, run_id)


def _live(cfg: Config, issue_id: str) -> dict:
    return linear.gql(cfg, ISSUE_QUERY, {"id": issue_id})["issue"]


def _state_id(cfg: Config, issue: dict, name: str, cache: dict) -> str:
    tid = issue["team"]["id"]
    if tid not in cache:
        cache[tid] = {s["name"]: s["id"] for s in linear.gql(cfg, STATES_QUERY, {"id": tid})["team"]["states"]["nodes"]}
    return cache[tid][name]


def apply(cfg: Config, conn, run_id: str) -> dict:
    """Send planned apply rows (state first: a comment would bump updatedAt), ask about held ones, close the run."""
    # 'sent' = an apply claimed the row and never finished: Linear may or may not have it. Never resend; a person
    # checks. ponytail: an apply running concurrently right now looks the same; its row still lands, and the
    # question is then moot.
    for w in conn.execute("SELECT * FROM writeback WHERE run_id=? AND status='sent'", (run_id,)).fetchall():
        key = (run_id, w["issue_id"], w["op"])
        reason = "sent but never confirmed (an apply stopped mid-write); check Linear before deciding"
        with db.tx(conn):
            conn.execute("UPDATE writeback SET decision='flag', reason=? WHERE run_id=? AND issue_id=? AND op=? "
                         "AND decision='apply'", (reason, *key))
            decide.writeback(conn, run_id, w["issue_id"], w["op"], json.loads(w["payload_json"]), reason)
            conn.execute("UPDATE writeback SET status='confirmed' WHERE run_id=? AND issue_id=? AND op=?", key)
    order = "CASE op WHEN 'state' THEN 0 WHEN 'description' THEN 1 WHEN 'create' THEN 2 ELSE 3 END"
    states: dict = {}
    touched: set[str] = set()
    for w in conn.execute(f"SELECT * FROM writeback WHERE run_id=? AND status IN ('planned','failed') "
                          f"ORDER BY issue_id, {order}", (run_id,)).fetchall():
        key = (run_id, w["issue_id"], w["op"])
        # Claim it first: a second apply (cron agent + manual run) must not send it too.
        if conn.execute("UPDATE writeback SET status='sent' WHERE run_id=? AND issue_id=? AND op=? "
                        "AND status IN ('planned','failed')", key).rowcount != 1:
            continue
        p = json.loads(w["payload_json"])
        decision, reason = w["decision"], w["reason"]
        try:
            if decision == "apply":
                issue = _live(cfg, w["issue_id"])
                touched.add(w["issue_id"])
                if w["op"] == "state":
                    if why := state_gate(cfg, issue, p.get("expect_updated_at")):
                        decision, reason = "flag", f"apply-time gate: {why}"
                        conn.execute("UPDATE writeback SET decision='flag', reason=? WHERE run_id=? AND issue_id=? "
                                     "AND op=?", (reason, *key))
                    else:
                        r = linear.gql(cfg, UPDATE, {"id": w["issue_id"], "input": {
                            "stateId": _state_id(cfg, issue, p["state"], states)}})["issueUpdate"]
                        ok = r["success"] and r["issue"]["state"]["name"] == p["state"]
                        conn.execute("UPDATE writeback SET status=?, linear_ref=? WHERE run_id=? AND issue_id=? AND op=?",
                                     ("confirmed" if ok else "failed", r["issue"]["updatedAt"], *key))
                elif w["op"] == "description":
                    desc = issue.get("description") or ""
                    new = (_COMPLETION.sub(lambda _: p["completion"].rstrip("\n") + "\n\n", desc, count=1)
                           if _COMPLETION.search(desc) else desc.rstrip("\n") + "\n\n" + p["completion"])
                    r = linear.gql(cfg, UPDATE, {"id": w["issue_id"], "input": {"description": new}})["issueUpdate"]
                    conn.execute("UPDATE writeback SET status=?, linear_ref=? WHERE run_id=? AND issue_id=? AND op=?",
                                 ("confirmed" if r["success"] else "failed", r["issue"]["updatedAt"], *key))
                elif w["op"] == "create":  # follow-up ticket; issue_id is the parent it was split from
                    r = linear.gql(cfg, CREATE, {"input": {"teamId": p["team_id"], "title": p["title"],
                                                           "description": p["description"],
                                                           "stateId": _state_id(cfg, {"team": {"id": p["team_id"]}},
                                                                                p["state"], states)}})["issueCreate"]
                    # Record the new id before relating it; the except below leaves a confirmed row alone, so a
                    # failed relation can never re-create the ticket (the relation is then simply missing).
                    conn.execute("UPDATE writeback SET status=?, linear_ref=? WHERE run_id=? AND issue_id=? AND op=?",
                                 ("confirmed" if r["success"] else "failed", r["issue"]["identifier"], *key))
                    linear.gql(cfg, RELATE, {"input": {"issueId": r["issue"]["id"], "relatedIssueId": w["issue_id"],
                                                       "type": "related"}})
                else:
                    r = linear.gql(cfg, COMMENT, {"input": {"issueId": w["issue_id"], "body": p["body"]}})
                    r = r["commentCreate"]
                    conn.execute("UPDATE writeback SET status=?, linear_ref=? WHERE run_id=? AND issue_id=? AND op=?",
                                 ("confirmed" if r["success"] else "failed", r["comment"]["id"], *key))
            if decision == "flag":  # held: the user decides (apply anyway / skip / do it in Linear)
                decide.writeback(conn, run_id, w["issue_id"], w["op"], p, reason)
            if decision in ("flag", "skip"):
                conn.execute("UPDATE writeback SET status='confirmed' WHERE run_id=? AND issue_id=? AND op=?", key)
        except Exception as e:  # one bad write never blocks the rest; failed rows retry on the next apply
            conn.execute("UPDATE writeback SET status='failed', reason=? WHERE run_id=? AND issue_id=? AND op=? "
                         "AND status='sent'", (f"{type(e).__name__}: {e}"[:400], *key))
    # Our own writes bump updatedAt. Record the result so prune does not treat it as a ticket change and
    # re-verify (which re-plans the same comment every sweep). ponytail: a human edit landing between our
    # write and this read is also absorbed; the next human edit re-arms it.
    for issue_id in touched:
        try:
            conn.execute("INSERT OR IGNORE INTO linear_own_write VALUES (?,?)",
                         (issue_id, _live(cfg, issue_id)["updatedAt"]))
        except Exception as e:  # the writes landed; losing this only costs one re-verification
            print(f"factory: reconcile: own-write read for {issue_id}: {type(e).__name__}: {e}", file=sys.stderr)
    return {**show(conn, run_id), "unfinished": close(cfg, conn, run_id)}


def close(cfg: Config, conn, run_id: str) -> int:
    """Close-out once every row landed: verdicts written back, done -> reconciled -> archived. Returns rows left."""
    with db.tx(conn):  # a verdict is written back once all of its rows landed; never re-planned after
        conn.execute("UPDATE verdict SET written_back_run=? WHERE written_back_run IS NULL AND id IN ("
                     "SELECT json_extract(payload_json, '$.verdict_id') v FROM writeback WHERE run_id=? AND v IS NOT NULL "
                     "GROUP BY v HAVING min(status = 'confirmed') = 1)", (run_id, run_id))
    left = conn.execute("SELECT count(*) FROM writeback WHERE run_id=? AND status <> 'confirmed'", (run_id,)).fetchone()[0]
    if left == 0 and conn.execute("UPDATE dispatch SET state='reconciled', reconciled_at=?, "
                                  "last_actor='factory:reconcile' WHERE run_id=? AND state='done'",
                                  (db.now(), run_id)).rowcount:
        try:  # right away: propose waits while any dispatch is reconciled; the gate retries a failure
            archive(cfg, conn, run_id)
        except Exception as e:
            print(f"factory: reconcile: archive {run_id}: {type(e).__name__}: {e}", file=sys.stderr)
    return left
