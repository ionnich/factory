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


def unresolved(conn) -> list:
    """The Reconcile stage's open writes across every run (dispatch, sweep-, followup-): planned, sent and failed ones
    (skips write nothing), and held ones whose decision is still open (decision_id). A pure read; nothing is applied."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM (SELECT l.identifier, w.run_id, w.op, w.status, w.decision, w.reason, (SELECT max(x.id) FROM "
        "decision x WHERE x.kind='writeback' AND x.run_id=w.run_id AND x.issue_id=w.issue_id AND x.ref=w.op AND "
        f"{decide.OPEN}) decision_id FROM writeback w LEFT JOIN linear_latest l USING (issue_id)) WHERE "
        "(status <> 'confirmed' AND decision <> 'skip') OR decision_id IS NOT NULL ORDER BY run_id, identifier, op")]


def resolve(conn, run_id: str, identifier: str, op: str, body: str | None, flag_reason: str | None) -> dict:
    """Agent edits: prose of comment/description rows, or downgrade apply -> flag. Nothing else, and never a write
    a person chose to apply."""
    w = conn.execute("SELECT w.* FROM writeback w JOIN linear_latest l USING (issue_id) "
                     "WHERE w.run_id=? AND l.identifier=? AND w.op=?", (run_id, identifier, op)).fetchone()
    if w is None or w["status"] != "planned":
        raise StageError(f"no planned {op} write for {identifier} in {run_id}")
    if (w["rule"] or "").startswith("domain-groom"):
        raise StageError("domain grooming writes are pinned by a person; the reconcile agent cannot edit or hold them")
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


def _domain_of_description(conn, description):
    """The canonical Domain project a description resolves to (its Domain: line), or None."""
    return prune.domain_project(conn, {"raw_json": json.dumps({"description": description or "",
                                                               "labels": {"nodes": []}}), "identifier": None})


def _domain_of_live(conn, issue: dict):
    """The canonical Domain project of a live Linear issue, resolved from its Domain: line (never issue.project)."""
    return prune.domain_project(conn, {"raw_json": json.dumps(issue), "identifier": issue.get("identifier")})


def _own_stamps(conn, run_id: str, issue_id: str) -> set[str]:
    """Exact updatedAt values this review's own confirmed+applied description/state writes produced for one issue."""
    return {r[0] for r in conn.execute(
        "SELECT linear_ref FROM writeback WHERE run_id=? AND issue_id=? AND op IN ('description','state') "
        "AND status='confirmed' AND decision='apply' AND linear_ref IS NOT NULL", (run_id, issue_id))}


def _own_canceled(conn, run_id: str, issue_id: str) -> bool:
    return conn.execute("SELECT 1 FROM writeback WHERE run_id=? AND issue_id=? AND op='state' "
                        "AND status='confirmed' AND decision='apply'", (run_id, issue_id)).fetchone() is not None


def _groom_dependency(conn, run_id: str, w, p: dict) -> str | None:
    """Why a groom write must WAIT (stay planned) for another actually-applied write in this run, or None. Durable:
    a separate apply invocation sees the same confirmed evidence."""
    if w["op"] == "state" and (w["rule"] or "").startswith("domain-groom-merge"):
        tid = p.get("target_issue_id")
        if tid and conn.execute("SELECT 1 FROM writeback WHERE run_id=? AND issue_id=? AND op='description' "
                                "AND rule='domain-groom-rewrite' AND status='confirmed' AND decision='apply'",
                                (run_id, tid)).fetchone() is None:
            return f"waiting for the target rewrite of {p.get('target')} to be confirmed"
    if w["op"] == "comment" and (w["rule"] or "").startswith("domain-groom"):
        if conn.execute("SELECT 1 FROM writeback WHERE run_id=? AND issue_id=? AND op='state' "
                        "AND status='confirmed' AND decision='apply'", (run_id, w["issue_id"])).fetchone() is None:
            return "waiting for the state write to be confirmed"
    return None


def _domain_gate(cfg: Config, conn, run_id: str, issue: dict, p: dict) -> str | None:
    """Why a domain-groom write is NOT allowed now, or None. Every check is re-run against a live read immediately
    before each write; this review's own confirmed+applied writes (durable, via writeback status/linear_ref) are
    excluded from the freshness/state checks so a multi-write ticket never stales its own remaining writes."""
    expect = p.get("expect_updated_at")
    if expect and issue["updatedAt"] != expect and issue["updatedAt"] not in _own_stamps(conn, run_id, issue["id"]):
        return f"ticket changed since the review ({expect} -> {issue['updatedAt']})"
    who = (issue["assignee"] or {}).get("email")
    if who not in (None, cfg.linear["lead"]):
        return f"assigned to {who}"
    if issue["state"]["type"] in ("completed", "canceled") and not _own_canceled(conn, run_id, issue["id"]):
        return f"already {issue['state']['name']}"
    if issue["state"]["name"] == team_cfg(cfg, issue).get("review_state"):
        return f"waiting on human review ({issue['state']['name']})"
    domain = _domain_of_live(conn, issue)
    if domain is None or domain["id"] != p.get("domain_id"):
        return "ticket left the reviewed domain"
    if domain["lead_email"] != cfg.linear["lead"]:
        return "domain no longer led by the factory lead"
    if conn.execute("SELECT 1 FROM dispatch_ticket t JOIN dispatch d USING (run_id) "
                    "WHERE t.issue_id=? AND d.state <> 'archived'", (issue["id"],)).fetchone():
        return "ticket is in a live dispatch"
    if p.get("description") is not None:
        proposed = _domain_of_description(conn, p["description"])
        if proposed is None or proposed["id"] != p.get("domain_id"):
            return "the proposed description drops or moves its Domain line"
    if p.get("target_issue_id"):
        target = _live(cfg, p["target_issue_id"])
        tname = p.get("target")
        if target["state"]["type"] in ("completed", "canceled"):
            return f"merge target {tname} is already closed"
        if (target["assignee"] or {}).get("email") not in (None, cfg.linear["lead"]):
            return f"merge target {tname} is assigned to {(target['assignee'] or {}).get('email')}"
        if target["state"]["name"] == team_cfg(cfg, target).get("review_state"):
            return f"merge target {tname} is waiting on human review"
        tdomain = _domain_of_live(conn, target)
        if tdomain is None or tdomain["id"] != p.get("domain_id"):
            return f"merge target {tname} left the reviewed domain"
        texpect = p.get("target_expect_updated_at")
        if texpect and target["updatedAt"] != texpect:
            # the target's own confirmed rewrite (exact returned timestamp) is the only allowed drift
            tconfirmed = conn.execute("SELECT linear_ref FROM writeback WHERE run_id=? AND issue_id=? AND "
                                      "op='description' AND rule='domain-groom-rewrite' AND status='confirmed' "
                                      "AND decision='apply'", (run_id, p["target_issue_id"])).fetchone()
            if tconfirmed is None or tconfirmed[0] != target["updatedAt"]:
                return f"merge target {tname} changed since the review"
        if conn.execute("SELECT 1 FROM dispatch_ticket t JOIN dispatch d USING (run_id) "
                        "WHERE t.issue_id=? AND d.state <> 'archived'", (p["target_issue_id"],)).fetchone():
            return f"merge target {tname} is in a live dispatch"
    return None


def _groom_write(cfg: Config, issue: dict, w, p: dict, states: dict) -> dict:
    """One pinned domain-groom mutation. Returns {ok, ref, updated_at} (updated_at is the exact Linear updatedAt the
    write produced, when the mutation reports it; comments report none). A rewrite carries its optional title and
    description together in one description row and one mutation."""
    if w["op"] == "description":
        inp = {}
        if p.get("title") is not None:
            inp["title"] = p["title"]
        if p.get("description") is not None:
            inp["description"] = p["description"]
        r = linear.gql(cfg, UPDATE, {"id": w["issue_id"], "input": inp})["issueUpdate"]
        return {"ok": r["success"], "ref": r["issue"]["updatedAt"], "updated_at": r["issue"]["updatedAt"]}
    if w["op"] == "state":
        r = linear.gql(cfg, UPDATE, {"id": w["issue_id"], "input": {
            "stateId": _state_id(cfg, issue, p["state"], states)}})["issueUpdate"]
        return {"ok": r["success"] and r["issue"]["state"]["name"] == p["state"],
                "ref": r["issue"]["updatedAt"], "updated_at": r["issue"]["updatedAt"]}
    if w["op"] == "comment":
        r = linear.gql(cfg, COMMENT, {"input": {"issueId": w["issue_id"], "body": p["body"]}})["commentCreate"]
        return {"ok": r["success"], "ref": r["comment"]["id"], "updated_at": None}
    raise StageError(f"domain groom op {w['op']} is not writable")


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
            decide.writeback(conn, run_id, w["issue_id"], w["op"], json.loads(w["payload_json"]), reason,
                             rule=w["rule"])
            conn.execute("UPDATE writeback SET status='confirmed' WHERE run_id=? AND issue_id=? AND op=?", key)
    # Groom rows order by dependency (rewrite -> cancel -> comment) so a merge's source cancel only runs after its
    # target rewrite is confirmed; legacy rows keep their per-issue state/description/create/comment order.
    order = ("CASE WHEN rule LIKE 'domain-groom%' "
             "THEN CASE op WHEN 'description' THEN 0 WHEN 'state' THEN 1 ELSE 2 END "
             "ELSE CASE op WHEN 'state' THEN 0 WHEN 'description' THEN 1 WHEN 'create' THEN 2 ELSE 3 END END")
    states: dict = {}
    touched: set[str] = set()
    for w in conn.execute(f"SELECT * FROM writeback WHERE run_id=? AND status IN ('planned','failed') "
                          f"ORDER BY {order}, issue_id, op", (run_id,)).fetchall():
        key = (run_id, w["issue_id"], w["op"])
        p = json.loads(w["payload_json"])
        groom = (w["rule"] or "").startswith("domain-groom")
        if groom and _groom_dependency(conn, run_id, w, p):
            continue  # dependency not yet applied: stay planned, re-evaluate on the next apply (never flag/send)
        # Claim it first: a second apply (cron agent + manual run) must not send it too.
        if conn.execute("UPDATE writeback SET status='sent' WHERE run_id=? AND issue_id=? AND op=? "
                        "AND status IN ('planned','failed')", key).rowcount != 1:
            continue
        decision, reason = w["decision"], w["reason"]
        try:
            if decision == "apply":
                issue = _live(cfg, w["issue_id"])
                if groom:
                    # A person froze this exact payload; every gate is re-checked live, and only the pinned content
                    # is sent (never reworded).
                    if why := _domain_gate(cfg, conn, run_id, issue, p):
                        decision, reason = "flag", f"apply-time gate: {why}"
                        conn.execute("UPDATE writeback SET decision='flag', reason=? WHERE run_id=? AND issue_id=? "
                                     "AND op=?", (reason, *key))
                    else:
                        try:
                            r = _groom_write(cfg, issue, w, p, states)
                        except Exception as e:
                            # uncertain send: the mutation may have landed. Hold it; never blindly resend.
                            reason2 = f"groom send uncertain: {type(e).__name__}: {e}"[:400]
                            with db.tx(conn):
                                conn.execute("UPDATE writeback SET decision='flag', reason=? WHERE run_id=? AND "
                                             "issue_id=? AND op=? AND status='sent'", (reason2, *key))
                                decide.writeback(conn, run_id, w["issue_id"], w["op"], p, reason2, rule=w["rule"])
                                conn.execute("UPDATE writeback SET status='confirmed' WHERE run_id=? AND issue_id=? "
                                             "AND op=?", key)
                            continue
                        else:
                            if r["ok"]:
                                with db.tx(conn):  # persist the exact returned updatedAt with the confirmation
                                    conn.execute("UPDATE writeback SET status='confirmed', linear_ref=? "
                                                 "WHERE run_id=? AND issue_id=? AND op=?", (r["ref"], *key))
                                    if r["updated_at"]:
                                        conn.execute("INSERT OR IGNORE INTO linear_own_write VALUES (?,?)",
                                                     (w["issue_id"], r["updated_at"]))
                            else:
                                conn.execute("UPDATE writeback SET status='failed' WHERE run_id=? AND issue_id=? "
                                             "AND op=?", key)
                else:
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
                decide.writeback(conn, run_id, w["issue_id"], w["op"], p, reason, rule=w["rule"])
            if decision in ("flag", "skip"):
                conn.execute("UPDATE writeback SET status='confirmed' WHERE run_id=? AND issue_id=? AND op=?", key)
        except Exception as e:  # one bad write never blocks the rest; failed rows retry on the next apply
            conn.execute("UPDATE writeback SET status='failed', reason=? WHERE run_id=? AND issue_id=? AND op=? "
                         "AND status='sent'", (f"{type(e).__name__}: {e}"[:400], *key))
    # Legacy writes re-read their own updatedAt after the fact (a human edit between the write and read is absorbed
    # there; the next human edit re-arms it). Domain grooming persists the exact returned timestamp inline instead.
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
