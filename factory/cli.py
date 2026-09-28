"""factory: control-plane CLI. Exit 0 ok, 1 refused/invalid, 2 config/infra error."""
import argparse
import hashlib
import json
import sys

from . import config, db, linear, prune, repos, witness


def out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_ingest(cfg, conn, a):
    res = linear.ingest(cfg, conn, full=a.full)
    res["projects"] = linear.sync_projects(cfg, conn)
    with db.tx(conn):
        res["trunks"] = {r: sha[:12] for r, sha in repos.sync_all(cfg, conn).items()}
    out(res)


def cmd_sync(cfg, conn, a):
    with db.tx(conn):
        out({r: sha for r, sha in repos.sync_all(cfg, conn).items()})


def cmd_status(cfg, conn, a):
    if a.run_id:
        return out(dispatch_status(cfg, conn, a.run_id))
    q = lambda sql, *p: [dict(r) for r in conn.execute(sql, p)]
    fresh = {"fresh": 0, "stale": 0, "unverified": 0}
    owned = prune.owned_in_scope(cfg, conn)
    for s in owned:
        ctx, _ = prune.map_context(cfg, s)
        why = prune.staleness(cfg, conn, s, ctx)
        fresh["fresh" if why is None else "unverified" if why == "new" else "stale"] += 1
    ids = [s["issue_id"] for s in owned]
    out({
        "sync": q("SELECT * FROM sync_cursor"),
        "trunks": q("SELECT repo, branch, substr(sha,1,12) sha, fetched_at FROM repo_trunk"),
        "lead": cfg.linear["lead"],
        "owned_tickets_in_scope": fresh,
        "ignored_other_leads": conn.execute("SELECT count(*) FROM linear_latest WHERE in_scope=1").fetchone()[0] - len(owned),
        "verdicts": q("SELECT kind, count(*) n FROM verdict WHERE superseded_at IS NULL AND issue_id IN "
                      "(SELECT value FROM json_each(?)) GROUP BY kind", json.dumps(ids)),
        "dispatches": q("SELECT run_id, state, staged_at, executing_at, done_at FROM dispatch "
                        "WHERE state <> 'archived' ORDER BY created_at"),
        "open_flags": q("SELECT id, run_id, issue_id, kind FROM flag WHERE resolved_at IS NULL"),
        "kanban": cfg.kanban,
    })


def dispatch_status(cfg, conn, run_id):
    d = conn.execute("SELECT * FROM dispatch WHERE run_id=?", (run_id,)).fetchone()
    if d is None:
        raise SystemExit(f"unknown dispatch {run_id}")
    base = cfg.dispatches / ("_archived" if d["state"] == "archived" else "") / run_id / "dispatch.md"
    body = base.read_bytes() if base.exists() else None
    return {
        **dict(d),
        "path": str(base),
        "hash_ok": None if d["body_sha256"] is None or body is None
        else hashlib.sha256(body).hexdigest() == d["body_sha256"],
        "tickets": [dict(r) for r in conn.execute(
            "SELECT identifier, card_status, kanban_card_id, pr_url FROM dispatch_ticket WHERE run_id=?", (run_id,))],
        "transitions": [dict(r) for r in conn.execute(
            "SELECT from_state, to_state, actor, at FROM transition_log WHERE run_id=? ORDER BY id", (run_id,))],
    }


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
    print(json.dumps(prune.gate(cfg, conn)))


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
    sub.add_parser("prune-gate", help="Hermes pre-check for the prune job").set_defaults(fn=cmd_prune_gate)
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
    except (prune.VerdictError, prune.NotOwned, witness.WitnessError) as e:
        print(f"factory: refused: {e}", file=sys.stderr)
        sys.exit(1)
    except config.ConfigError as e:
        print(f"factory: config: {e}", file=sys.stderr)
        sys.exit(2)
