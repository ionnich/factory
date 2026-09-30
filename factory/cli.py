"""factory: control-plane CLI. Exit 0 ok, 1 refused/invalid, 2 config/infra error."""
import argparse
import collections
import hashlib
import json
import re
import sqlite3
import statistics
import sys
from datetime import UTC, datetime, timedelta

from . import ask, config, costs, db, decide, dispatch, learn, linear, prune, reconcile, repos, witness


def out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def ingest(cfg, conn, full=False, only: set[str] | None = None) -> dict:
    """Linear (incremental) and the trunk mirrors. `only`: just these repos and no project refresh, the fast path
    before drafting or approving (the cron keeps everything else fresh)."""
    res = linear.ingest(cfg, conn, full=full)
    if only is None:
        res["projects"] = linear.sync_projects(cfg, conn)
    res["trunks"] = {r: sha[:12] for r, sha in repos.sync_all(cfg, conn, only).items()}
    return res


def _repos_of(cfg, conn, identifiers) -> set[str]:
    """Repos the named tickets map to now (unknown ones are refused later, with the reason)."""
    found = set()
    for i in identifiers:
        s = conn.execute("SELECT * FROM linear_latest WHERE identifier=?", (i,)).fetchone()
        if s is not None and (ctx := prune.map_context(cfg, s)[0]):
            found.add(ctx.repo)
    return found


def cmd_ingest(cfg, conn, a):
    out(ingest(cfg, conn, a.full))


def cmd_candidates(cfg, conn, a):
    out(dispatch.candidates(cfg, conn))


def cmd_stage(cfg, conn, a):
    ingest(cfg, conn, only=_repos_of(cfg, conn, a.identifiers))  # stage against Linear and trunk as they are now
    out(dispatch.stage(cfg, conn, a.identifiers, a.actor))


def cmd_card(cfg, conn, a):
    out(dispatch.card(cfg, conn, a.run_id, a.identifier, a.kind, a.actor, body=a.body, pr=a.pr))


def cmd_execute(cfg, conn, a):
    out(dispatch.execute(cfg, conn, a.run_id, a.actor))


def cmd_handoff(cfg, conn, a):
    out(dispatch.handoff(cfg, conn, a.run_id))


def cmd_propose(cfg, conn, a):
    """Cron: ingest, move dispatches along, take ★ where its time came, and say what the user needs to hear."""
    ingest(cfg, conn)
    res = dispatch.propose(cfg, conn)
    res["swept"] = decide.sweep(cfg, conn)
    msgs = decide.notify(cfg, conn, res["swept"])
    try:  # the cost ledger is derived data: a failed sync never costs the user a push or digest
        costs.sync(conn, cfg.db.parent)
    except Exception as e:
        print(f"factory: cost sync: {type(e).__name__}: {e}", file=sys.stderr)
    try:  # learnings are derived too; proposals it opens reach the user in the next digest
        learn.sync(cfg, conn)
    except Exception as e:
        print(f"factory: learn sync: {type(e).__name__}: {e}", file=sys.stderr)
    if not a.announce:
        return out({**res, "messages": msgs})
    if msgs:  # cron stdout -> bot-chat:factory (Hermex); nothing to say = no message
        print("\n\n".join(msgs))


def cmd_draft(cfg, conn, a):
    if a.dcmd == "gate":  # Hermes pre-check for factory-plan: drafts without a plan, one per run
        d = conn.execute("SELECT run_id FROM dispatch WHERE state='draft' AND planned_at IS NULL "
                         "ORDER BY created_at LIMIT 1").fetchone()
        ctx = dispatch_status(cfg, conn, d["run_id"]) if d else None
        if ctx:
            ctx["tickets"] = [json.loads(json.dumps(t, default=str)) for t in dispatch._tickets_for_render(
                cfg, conn, d["run_id"], check=False)[0]]
            for t in ctx["tickets"]:
                t["mirror"] = str(cfg.mirror_path(t["repo"]))
            ctx["learnings"] = learn.relevant(
                conn, {t["repo"] for t in ctx["tickets"]},
                [e["path"] for t in ctx["tickets"] for e in t["evidence"] if e.get("type") == "file"],
                "\n".join(f"{t['title']}\n{t['description']}" for t in ctx["tickets"]))
            # notes on steps a replan cleared: gone from the tree, still binding (answered questions: `decisions`)
            ids = {n["id"] for n in ctx["tree"]}
            ctx["earlier_notes"] = [dict(r) for r in conn.execute(
                "SELECT node_id, author, body, at FROM dispatch_note WHERE run_id=? ORDER BY id", (d["run_id"],))
                if r["node_id"] not in ids]
        return print(json.dumps({"wakeAgent": bool(ctx), "context": {"draft": ctx}}, default=str))
    if a.dcmd == "plan":
        try:
            steps = json.loads(a.steps)
        except json.JSONDecodeError as e:
            raise dispatch.StageError(f"--steps is not JSON: {e}")
        return out(dispatch.plan(cfg, conn, a.run_id, steps))
    if a.dcmd == "replan":
        return out(dispatch.replan(conn, a.run_id, a.reason, a.actor))
    return out(dispatch.note(conn, a.run_id, a.node, a.body, a.actor))


def cmd_decide(cfg, conn, a):
    if a.xcmd == "list":
        return out(decide.rows(conn, a.run_id, open_only=not a.all))
    if a.xcmd == "ask":
        opts = []
        for o in a.option:
            parts = [p.strip() for p in o.split("|")]
            if len(parts) != 3 or not all(parts):
                raise dispatch.StageError(f"--option {o!r}: want 'id|label|what it leads to'")
            opts.append(decide.option(*parts))
        return out(decide.ask(conn, a.run_id, a.node, a.question, opts, a.recommend, a.why, a.actor))
    if a.xcmd == "ok":
        runs = [d["run_id"] for i in a.ids if (d := decide.one(conn, i)) and d["kind"] == "review"
                and d["recommended"] == "approve"]
        if runs:
            ingest(cfg, conn, only=_dispatch_repos(conn, runs))
        return out(decide.ok(cfg, conn, a.ids, a.actor))
    d = decide.one(conn, a.id)
    if d and d["kind"] == "review" and a.option == "approve":
        ingest(cfg, conn, only=_dispatch_repos(conn, [d["run_id"]]))  # check the tickets against Linear as it is now
    return out(decide.choose(cfg, conn, a.id, a.option, a.actor, a.note))


def _dispatch_repos(conn, run_ids) -> set[str]:
    return {r["repo"] for (j,) in conn.execute(f"SELECT repos_json FROM dispatch WHERE run_id IN "
                                               f"({','.join('?' * len(run_ids))})", run_ids) for r in json.loads(j)}


def cmd_backup(cfg, conn, a):
    out(db.backup(conn, cfg.db.parent / "factory" / "backups", f"{datetime.now().astimezone():%Y-%m-%d}", a.keep))


def _p50(xs):
    return round(statistics.median(xs), 1) if xs else None


def cmd_metrics(cfg, conn, a):
    """Throughput over the last N days, from transition_log, dispatch_ticket, writeback and verdict."""
    since = (datetime.now(UTC) - timedelta(days=a.days)).strftime("%Y-%m-%dT%H:%M:%S")
    q = lambda sql, *p: [dict(r) for r in conn.execute(sql, p)]
    at = {(r["run_id"], r["to_state"]): r["at"] for r in q("SELECT run_id, to_state, at FROM transition_log")}
    hours = lambda a_, b: (datetime.fromisoformat(b) - datetime.fromisoformat(a_)).total_seconds() / 3600
    staged = [r for (r, s), t in at.items() if s == "staged" and t >= since]
    to_done = [hours(at[(r, "staged")], at[(r, "done")]) for r in staged if (r, "done") in at]
    to_arch = [hours(at[(r, "done")], at[(r, "archived")]) for r in staged if (r, "done") in at and (r, "archived") in at]
    tickets = q("SELECT t.run_id, t.card_status, v.repo, e.at FROM dispatch_ticket t JOIN verdict v ON v.id=t.verdict_id "
                "JOIN card_event e ON e.run_id=t.run_id AND e.issue_id=t.issue_id AND e.kind IN ('done','block') "
                "WHERE e.at >= ?", since)
    n = collections.Counter(t["card_status"] for t in tickets)
    week = lambda ts: (datetime.fromisoformat(ts).date() - timedelta(days=datetime.fromisoformat(ts).weekday())).isoformat()
    weeks = collections.defaultdict(collections.Counter)
    for r in staged:
        weeks[week(at[(r, "staged")])]["staged"] += 1
    for t in tickets:
        weeks[week(t["at"])][t["card_status"]] += 1
    repos_ = collections.defaultdict(collections.Counter)
    for t in tickets:
        repos_[t["repo"]][t["card_status"]] += 1
    wb = collections.Counter(  # writeback has no timestamp; every run_id embeds its YYYYMMDD-HHMMSS
        "flagged" if r["decision"] == "flag" else r["status"]
        for r in q("SELECT run_id, decision, status FROM writeback WHERE decision <> 'skip'")
        if (m := re.search(r"(\d{8})-\d{6}", r["run_id"])) and m[1] >= since[:10].replace("-", ""))
    out({"days": a.days,
         "dispatches": {"staged": len(staged), "archived": sum((r, "archived") in at for r in staged)},
         "tickets": {"done": n["done"], "blocked": n["blocked"]},
         "block_rate": round(n["blocked"] / (n["done"] + n["blocked"]), 2) if n["done"] + n["blocked"] else None,
         "hours": {"stage_to_done_p50": _p50(to_done), "stage_to_done_max": round(max(to_done), 1) if to_done else None,
                   "done_to_archived_p50": _p50(to_arch)},
         "per_week": [{"week": w, "staged": c["staged"], "done": c["done"], "blocked": c["blocked"]}
                      for w, c in sorted(weeks.items())],
         "per_repo": [{"repo": r, "done": c["done"], "blocked": c["blocked"]} for r, c in sorted(repos_.items())],
         "writeback": {k: wb.get(k, 0) for k in ("confirmed", "failed", "flagged")},
         "verdicts": {r["kind"]: r["n"] for r in q("SELECT kind, count(*) n FROM verdict WHERE created_at >= ? "
                                                   "GROUP BY kind", since)},
         "cost": costs.summary(conn, a.days)})


def cmd_reconcile(cfg, conn, a):
    if a.rcmd == "plan":
        ingest(cfg, conn)  # Linear + trunk as they are now
        return out(reconcile.plan(cfg, conn, a.run_id))
    if a.rcmd == "resolve":
        return out(reconcile.resolve(conn, a.run_id, a.identifier, a.op, a.body, a.flag))
    if a.rcmd == "followup":
        return out(reconcile.followup(cfg, conn, a.parent, a.title, a.body, a.repo, a.actor))
    if a.rcmd == "apply":
        res = reconcile.apply(cfg, conn, a.run_id)
        out(res)
        return 1 if res["unfinished"] else 0
    # gate: archive reconciled dispatches, plan every done dispatch + a verdict sweep, close out done runs with
    # nothing left to send, and wake the agent for runs with unsent rows. One bad run never stops the others.
    for (run_id,) in conn.execute("SELECT run_id FROM dispatch WHERE state='reconciled'").fetchall():
        try:
            dispatch.archive(cfg, conn, run_id)
        except Exception as e:
            print(f"factory: gate: archive {run_id}: {type(e).__name__}: {e}", file=sys.stderr)
    dispatch.watch(cfg, conn)  # stuck / executor-gone decisions
    try:
        ingest(cfg, conn)
    except OSError as e:  # network down (URLError, HTTPError, timeout): skip this tick, no agent run
        return _gate_offline(e)
    for (run_id,) in conn.execute("SELECT run_id FROM dispatch WHERE state='done'").fetchall():
        try:
            reconcile.plan(cfg, conn, run_id)
            reconcile.close(cfg, conn, run_id)
        except Exception as e:
            print(f"factory: gate: {run_id}: {type(e).__name__}: {e}", file=sys.stderr)
    reconcile.plan(cfg, conn, None)
    runs = [reconcile.show(conn, r) for (r,) in conn.execute(
        "SELECT DISTINCT run_id FROM writeback WHERE status <> 'confirmed' ORDER BY run_id").fetchall()]
    print(json.dumps({"wakeAgent": bool(runs), "context": {"runs": runs}}, default=str))


def _gate_offline(e: OSError) -> None:
    print(json.dumps({"wakeAgent": False, "context": {"error": f"{type(e).__name__}: {e}"}}))


def cmd_archive(cfg, conn, a):
    out(dispatch.archive(cfg, conn, a.run_id))


def cmd_sync(cfg, conn, a):
    out({r: sha for r, sha in repos.sync_all(cfg, conn).items()})


def cmd_status(cfg, conn, a):
    out(dispatch_status(cfg, conn, a.run_id) if a.run_id else status(cfg, conn))


def cmd_overview(cfg, conn, a):
    """Everything the Factory tab shows, in one call: board, tickets, candidates, and each live dispatch (plus
    finished ones something still waits on the user for, e.g. a blocked ticket)."""
    st = status(cfg, conn)
    runs = dict.fromkeys([d["run_id"] for d in st["dispatches"]] +
                         [x["run_id"] for x in st["decisions"] if x["run_id"] and x["kind"] == "blocked"] +
                         [x["run_id"] for x in st["archived"]])  # last closed ones, for the Learn tab
    dispatches = [dispatch_status(cfg, conn, r) for r in runs
                  if conn.execute("SELECT 1 FROM dispatch WHERE run_id=?", (r,)).fetchone()]
    shown = [x["id"] for x in st["decisions"]] + [x["id"] for d in dispatches for x in d["decisions"]]
    out({"status": st, "tickets": tickets(cfg, conn), "candidates": dispatch.candidates(cfg, conn),
         "learnings": learn.rows(conn),
         "dispatches": dispatches, "asks": ask.rows(conn, set(shown))})  # "why?" threads by decision id


def status(cfg, conn) -> dict:
    q = lambda sql, *p: [dict(r) for r in conn.execute(sql, p)]
    fresh = {"fresh": 0, "stale": 0, "unverified": 0}
    owned = prune.owned_in_scope(cfg, conn)
    for s in owned:
        ctx, _ = prune.map_context(cfg, s)
        why = prune.staleness(cfg, conn, s, ctx)
        fresh["fresh" if why is None else "unverified" if why == "new" else "stale"] += 1
    ids = [s["issue_id"] for s in owned]
    week = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "sync": q("SELECT * FROM sync_cursor"),
        "trunks": q("SELECT repo, branch, substr(sha,1,12) sha, fetched_at FROM repo_trunk"),
        "lead": cfg.linear["lead"],
        "owned_tickets_in_scope": fresh,
        "ignored_other_leads": conn.execute("SELECT count(*) FROM linear_latest WHERE in_scope=1").fetchone()[0] - len(owned),
        "verdicts": q("SELECT kind, count(*) n FROM verdict WHERE superseded_at IS NULL AND issue_id IN "
                      "(SELECT value FROM json_each(?)) GROUP BY kind", json.dumps(ids)),
        "dispatches": q("SELECT run_id, state, staged_at, executing_at, done_at FROM dispatch "
                        "WHERE state <> 'archived' ORDER BY created_at"),
        "decisions": decide.rows(conn),  # everything waiting on a person, with options and a recommendation
        "done_for_you": decide.done_for_you(conn, week),  # what the factory answered on its own this week
        "kanban": cfg.kanban,
        # Last stage of the lifecycle: what the factory closed out recently.
        "archived": q("SELECT d.run_id, d.archived_at, group_concat(t.identifier, ', ') tickets, "
                      "sum(t.card_status='done') done, sum(t.card_status='blocked') blocked FROM dispatch d "
                      "JOIN dispatch_ticket t USING (run_id) WHERE d.state='archived' GROUP BY d.run_id "
                      "ORDER BY d.archived_at DESC LIMIT 5"),
        "written_back": q("SELECT l.identifier, json_extract(l.raw_json, '$.title') title, "
                          "json_extract(l.raw_json, '$.url') url, json_extract(l.raw_json, '$.state.name') linear_state, "
                          "v.kind, v.target, v.written_back_run run_id FROM verdict v JOIN linear_latest l USING (issue_id) "
                          "WHERE v.written_back_run LIKE 'sweep-%' AND v.superseded_at IS NULL "
                          "ORDER BY v.written_back_run DESC LIMIT 10"),
    }


def dispatch_status(cfg, conn, run_id):
    d = conn.execute("SELECT * FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if d is None:
        raise SystemExit(f"unknown dispatch {run_id}")
    base = cfg.dispatches / ("_archived" if d["state"] == "archived" else "") / run_id / "dispatch.md"
    body = base.read_bytes() if base.exists() else None
    rd = decide.open_review(conn, run_id)
    due = rd["due_at"] if rd else None
    return {
        **dict(d),
        "auto": d["drafted_by"] == dispatch.PROPOSE,
        "review": dispatch.review(d, due),
        "review_until": due,  # when its review takes ★ without the user
        "tree": dispatch.tree(conn, run_id),
        "path": str(base),
        "hash_ok": None if d["body_sha256"] is None or body is None
        else hashlib.sha256(body).hexdigest() == d["body_sha256"],
        "tickets": [dict(r) for r in conn.execute(
            "SELECT identifier, card_status, kanban_card_id, pr_url FROM dispatch_ticket WHERE run_id=?", (run_id,))],
        "transitions": [dict(r) for r in conn.execute(
            "SELECT from_state, to_state, actor, at FROM transition_log WHERE run_id=? ORDER BY id", (run_id,))],
        "decisions": decide.rows(conn, run_id, open_only=False),
        "writes": reconcile.show(conn, run_id)["writes"],  # what reconcile wrote (or holds) in Linear
        # what the executor reported per card: step progress ("FIN-1/2 done") and the done summary, for the outline
        "events": [dict(r) for r in conn.execute(
            "SELECT t.identifier, e.kind, e.body, e.at FROM card_event e JOIN dispatch_ticket t USING (run_id, issue_id) "
            "WHERE e.run_id=? AND e.kind IN ('comment', 'done', 'block') ORDER BY e.id", (run_id,))],
    }


def cmd_tickets(cfg, conn, a):
    out(tickets(cfg, conn))


def tickets(cfg, conn) -> list:
    """Owned in-scope tickets with verdict, freshness and live dispatch, for the status tab."""
    live = {r["issue_id"]: dict(r) for r in conn.execute(
        "SELECT t.issue_id, t.run_id, t.card_status, d.state FROM dispatch_ticket t JOIN dispatch d USING (run_id) "
        "WHERE d.state <> 'archived'")}
    rows = []
    for s in prune.owned_in_scope(cfg, conn):
        raw = json.loads(s["raw_json"])
        ctx, why = prune.map_context(cfg, s)
        v = conn.execute("SELECT kind, target, reason, evidence_json, created_at, created_by FROM verdict "
                         "WHERE issue_id=? AND superseded_at IS NULL", (s["issue_id"],)).fetchone()
        rows.append({
            "identifier": raw["identifier"], "title": raw["title"], "url": raw["url"],
            "domain": prune.domain_project(conn, s)["name"], "context": ctx.name if ctx else None,
            "unmapped_reason": why, "repo": ctx.repo if ctx else None,
            "linear_state": raw["state"]["name"], "assignee": (raw["assignee"] or {}).get("email"),
            "priority": raw["priority"], "updated_at": s["updated_at"],
            "freshness": prune.staleness(cfg, conn, s, ctx) or "fresh",
            "verdict": {**dict(v), "evidence": json.loads(v["evidence_json"])} if v else None,
            "dispatch": live.get(s["issue_id"]),
        })
    for r in rows:
        if r["verdict"]:
            del r["verdict"]["evidence_json"]
    return rows


def cmd_ticket(cfg, conn, a):
    s = prune.latest(conn, a.identifier)
    raw = json.loads(s["raw_json"])
    ctx, why = prune.map_context(cfg, s)
    v = conn.execute("SELECT * FROM verdict WHERE issue_id=? AND superseded_at IS NULL", (s["issue_id"],)).fetchone()
    trunk = conn.execute("SELECT sha FROM repo_trunk WHERE repo=?", (ctx.repo,)).fetchone() if ctx else None
    out({
        "identifier": raw["identifier"], "title": raw["title"], "url": raw["url"],
        "state": raw["state"]["name"], "assignee": (raw["assignee"] or {}).get("email"),
        "domain": prune.issue_fields(s)[0], "repo_lines": prune.issue_fields(s)[2],
        "domain_lead": (prune.domain_project(conn, s) or {"lead_email": None})["lead_email"],
        "owned": prune.owned(cfg, conn, s),
        "project": (raw["project"] or {}).get("name"), "milestone": (raw["projectMilestone"] or {}).get("name"),
        "labels": [x["name"] for x in raw["labels"]["nodes"]], "updated_at": s["updated_at"],
        "attachments": [x["url"] for x in raw["attachments"]["nodes"]],
        "context": ctx.name if ctx else None, "unmapped_reason": why,
        "repo": ctx.repo if ctx else None, "mirror": str(cfg.mirror_path(ctx.repo)) if ctx else None,
        "trunk_sha": trunk["sha"] if trunk else None, "witnesses": ctx.witnesses if ctx else [],
        "verdict_staleness": prune.staleness(cfg, conn, s, ctx),
        "current_verdict": dict(v) if v else None,
        "description": raw.get("description") or "",
    })


def cmd_prune_gate(cfg, conn, a):
    # Hermes reads the LAST stdout line as the wakeAgent gate.
    try:
        print(json.dumps(prune.gate(cfg, conn)))
    except OSError as e:
        _gate_offline(e)


def cmd_verdict_put(cfg, conn, a):
    src = a.evidence.strip()
    try:
        evidence = json.loads(src if src.startswith("[") else sys.stdin.read() if src == "-" else open(src).read())
    except (json.JSONDecodeError, OSError) as e:
        raise prune.VerdictError(f"evidence: {e}") from None
    vid = prune.put(cfg, conn, a.identifier, a.kind, a.reason, evidence, target=a.target, actor=a.actor)
    out({"verdict_id": vid})


def cmd_witness(cfg, conn, a):
    res = witness.run(cfg, conn, a.name, a.query)
    out(res)
    return 0 if res["ok"] else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="factory")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ingest", help="pull Linear into linear_snapshot and sync trunk mirrors")
    s.add_argument("--full", action="store_true", help="re-pull all active tickets, ignoring the cursor")
    s.set_defaults(fn=cmd_ingest)
    sub.add_parser("sync", help="sync trunk mirrors only").set_defaults(fn=cmd_sync)
    s = sub.add_parser("status", help="board summary, or one dispatch")
    s.add_argument("run_id", nargs="?")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("ticket", help="latest snapshot + mapping + current verdict")
    s.add_argument("identifier")
    s.set_defaults(fn=cmd_ticket)
    sub.add_parser("tickets", help="owned tickets with verdict + freshness (JSON)").set_defaults(fn=cmd_tickets)
    sub.add_parser("overview", help="everything the Factory tab shows, in one call (JSON)").set_defaults(fn=cmd_overview)
    sub.add_parser("prune-gate", help="Hermes pre-check for the prune job").set_defaults(fn=cmd_prune_gate)
    sub.add_parser("candidates", help="stageable tickets (JSON), and why the rest are not").set_defaults(
        fn=cmd_candidates)
    s = sub.add_parser("stage", help="ingest, then draft a dispatch for review (nothing starts until approved)")
    s.add_argument("identifiers", nargs="+")
    s.add_argument("--actor", default="user")
    s.set_defaults(fn=cmd_stage)
    s = sub.add_parser("handoff", help="reset the executor's omp session (/new) and tell it to run the dispatch")
    s.add_argument("run_id")
    s.set_defaults(fn=cmd_handoff)
    s = sub.add_parser("propose", help="cron: draft the top `auto` candidate, hand off approved dispatches, take ★ on "
                                        "decisions whose time came, and tell the user (push / digest)")
    s.add_argument("--announce", action="store_true", help="print only the messages for the user (cron delivery)")
    s.set_defaults(fn=cmd_propose)
    dr = sub.add_parser("draft", help="a draft dispatch's plan and notes (approve/hold/reject: `decide`)").add_subparsers(
        dest="dcmd", required=True)
    s = dr.add_parser("plan", help="planner agent: write the plan tree once")
    s.add_argument("run_id")
    s.add_argument("--steps", required=True, help='JSON list: [{"id":"FIN-1/1","title":..,"detail":..,"depends_on":[],'
                   '"files":[..]}, ...] (full shape: factory-plan skill, dispatch.plan)')
    s = dr.add_parser("note", help="add a note to a node (root, a ticket id, or a step id); binds the executor")
    s.add_argument("run_id")
    s.add_argument("--node", default="root")
    s.add_argument("--body", required=True)
    s.add_argument("--actor", default="user")
    s = dr.add_parser("replan", help="send a planned draft back to the planner with a reason (a binding root note)")
    s.add_argument("run_id")
    s.add_argument("--reason", required=True)
    s.add_argument("--actor", default="user")
    dr.add_parser("gate", help="Hermes pre-check for the factory-plan job (last line = wakeAgent JSON)")
    sub.choices["draft"].set_defaults(fn=cmd_draft)
    x = sub.add_parser("decide", help="decisions waiting on a person: options, what each leads to, a recommendation"
                       ).add_subparsers(dest="xcmd", required=True)
    s = x.add_parser("list", help="open decisions (JSON), or all of one dispatch with --all")
    s.add_argument("run_id", nargs="?")
    s.add_argument("--all", action="store_true", help="answered and withdrawn ones too")
    s = x.add_parser("choose", help="answer a decision; its effect runs (review approve starts the dispatch)")
    s.add_argument("id", type=int)
    s.add_argument("option")
    s.add_argument("--note", help="the text an option asks for (a reason, guidance)")
    s.add_argument("--actor", default="user")
    s = x.add_parser("ask", help="executor: ask the captain mid-run; the answer is typed into the executor pane")
    s.add_argument("run_id")
    s.add_argument("--node", default="root", help="root, a ticket id or a step id")
    s.add_argument("--question", required=True)
    s.add_argument("--option", action="append", required=True, help="'id|label|what it leads to' (2-5 times)")
    s.add_argument("--recommend", required=True, help="the option id you recommend")
    s.add_argument("--why", required=True, help="why you recommend it")
    s.add_argument("--actor", default="executor")
    s = x.add_parser("ok", help="the user's \"ok\": take the recommendation (★) on each of these decisions")
    s.add_argument("ids", type=int, nargs="+")
    s.add_argument("--actor", default="user")
    sub.choices["decide"].set_defaults(fn=cmd_decide)
    s = sub.add_parser("ask", help="\"why?\" on a decision: the planner explains inline").add_subparsers(
        dest="acmd", required=True)
    s2 = s.add_parser("new", help="ask; spawns the planner detached (one pending ask per decision)")
    s2.add_argument("decision_id", type=int)
    s2.add_argument("--text", required=True)
    s2.add_argument("--actor", default="user")
    s2 = s.add_parser("run", help="the detached wrapper: run the planner for a pending ask, store the result")
    s2.add_argument("id", type=int)
    s2 = s.add_parser("answer", help="store the answer of a pending ask")
    s2.add_argument("id", type=int)
    g = s2.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--file")
    s2.add_argument("--session", help="the Hermes session to resume for follow-ups")
    s2 = s.add_parser("list", help="one decision's thread (JSON)")
    s2.add_argument("decision_id", type=int)
    sub.choices["ask"].set_defaults(fn=lambda cfg, conn, a: out(ask.cli(cfg, conn, a)))
    s = sub.add_parser("backup", help="consistent, integrity-checked copy of factory.db; keeps the newest N")
    s.add_argument("--keep", type=int, default=14)
    s.set_defaults(fn=cmd_backup)
    s = sub.add_parser("metrics", help="throughput over the last N days (JSON)")
    s.add_argument("--days", type=int, default=28)
    s.set_defaults(fn=cmd_metrics)
    s = sub.add_parser("execute", help="staged -> executing; only from the executor's herdr workspace")
    s.add_argument("run_id")
    s.add_argument("--actor", default="executor")
    s.set_defaults(fn=cmd_execute)
    s = sub.add_parser("card", help="report on one card of the executing dispatch")
    s.add_argument("kind", choices=("claim", "comment", "done", "block"))
    s.add_argument("run_id")
    s.add_argument("identifier")
    s.add_argument("--body", help="comment text, block reason, or done summary")
    s.add_argument("--pr", help="PR URL in the ticket's repo (required for done; CI must be green)")
    s.add_argument("--actor", default="executor")
    s.set_defaults(fn=cmd_card)
    r = sub.add_parser("reconcile", help="write results back to Linear").add_subparsers(dest="rcmd", required=True)
    s = r.add_parser("plan", help="ingest, then plan writes for a done dispatch, or a verdict sweep without run_id")
    s.add_argument("run_id", nargs="?")
    s = r.add_parser("resolve", help="agent: rewrite comment/description prose, or downgrade apply -> flag")
    s.add_argument("run_id")
    s.add_argument("identifier")
    s.add_argument("--op", required=True, choices=("state", "comment", "description", "create"))
    s.add_argument("--body", help="new prose; must keep every URL, commit and ticket id of the draft")
    s.add_argument("--flag", help="downgrade this write to a flag for the user, with the reason")
    s = r.add_parser("apply", help="send planned writes (gates re-checked live), ask about held ones, close the run")
    s.add_argument("run_id")
    s = r.add_parser("followup", help="queue a new ticket split out of an owned one; reconcile creates it")
    s.add_argument("parent")
    s.add_argument("--title", required=True)
    s.add_argument("--body", required=True, help="markdown; the parent's Domain line and a split-out note are added")
    s.add_argument("--repo", help="adds a `Repo:` line so the new ticket maps to that repo")
    s.add_argument("--actor", default="user")
    r.add_parser("gate", help="Hermes pre-check for the reconcile job (last line = wakeAgent JSON)")
    sub.choices["reconcile"].set_defaults(fn=cmd_reconcile)
    s = sub.add_parser("archive", help="reconciled -> archived; move to _archived/ and commit")
    s.add_argument("run_id")
    s.set_defaults(fn=cmd_archive)
    v = sub.add_parser("verdict").add_subparsers(dest="vcmd", required=True)
    s = v.add_parser("put", help="record a verdict with evidence (JSON list from file or -)")
    s.add_argument("identifier")
    s.add_argument("--kind", required=True, choices=prune.KINDS)
    s.add_argument("--target")
    s.add_argument("--reason", required=True)
    s.add_argument("--evidence", required=True, help="inline JSON list, a path to one, or - for stdin")
    s.add_argument("--actor", default="agent:factory-prune")
    s.set_defaults(fn=cmd_verdict_put)
    s = sub.add_parser("witness", help="read-only query against a configured witness (logged)")
    s.add_argument("name")
    s.add_argument("query")
    s.set_defaults(fn=cmd_witness)

    a = p.parse_args(argv)
    try:
        cfg = config.load()
        conn = db.connect(cfg.db)
        sys.exit(a.fn(cfg, conn, a) or 0)
    except (prune.VerdictError, prune.NotOwned, witness.WitnessError, dispatch.StageError,
            sqlite3.IntegrityError) as e:  # IntegrityError = a schema trigger refused the write
        print(f"factory: refused: {e}", file=sys.stderr)
        sys.exit(1)
    except config.ConfigError as e:
        print(f"factory: config: {e}", file=sys.stderr)
        sys.exit(2)
