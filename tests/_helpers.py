"""Shared data fixtures for Execution tests. No schema DDL here: the merged Briefs schema (db.connect) is
authoritative. Briefs are published through the real strategy.create/approve; verdicts and the exact-version
association are written to the real tables (what prune.put does, minus the git-mirror evidence check a test cannot
reproduce)."""
import json
from pathlib import Path

from factory import db

SNAP = "2026-09-01T00:00:00Z"
HUMAN = "user:cli"


def mkcfg(tmp: Path):
    from factory.config import Config, Context
    return Config(raw={"linear": {"lead": "lead@x", "team": {"FIN": {"review_state": "Ready for QA"}}}},
                  db=tmp / "t.db", mirrors=tmp, dispatches=tmp / "dispatches",
                  contexts=[Context("api", "o/api", domains=["API"], route="fx-api")], repos={}, witnesses={})


def seed_project(c, domain="API", lead="lead@x"):
    c.execute("INSERT INTO linear_project VALUES ('p','s',?,?,?)", (domain, lead, SNAP))


def seed_trunk(c, repo="o/api", sha="deadbeef"):
    c.execute("INSERT INTO repo_trunk VALUES (?,?,?,?)", (repo, "main", sha, SNAP))


def seed_snapshot(c, issue_id, ident, snap=SNAP, state_type="unstarted", domain="API", in_scope=1, assignee=None,
                  state_name="Todo"):
    raw = json.dumps({"identifier": ident, "title": f"Fix {ident}", "url": f"https://linear.app/j/{ident}",
                      "state": {"name": state_name, "type": "unstarted"}, "team": {"key": "FIN", "id": "team"},
                      "assignee": assignee, "priority": 2, "description": f"Domain: {domain}",
                      "labels": {"nodes": []}})
    c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,?,?,?)",
              (issue_id, ident, snap, snap, state_type, in_scope, raw))


def seed_verdict(c, issue_id, snap=SNAP, context="api", repo="o/api"):
    cur = c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,context,repo,kind,reason,evidence_json,"
                    "evidence_paths_json,created_at,created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (issue_id, snap, context, repo, "valid", "r", "[1]", "[]", db.now(), "t"))
    return cur.lastrowid


def body(title="T", dependencies=(), resources=()):
    return {"title": title, "outcome": "done", "acceptance": ["a"], "scope": ["s"],
            "exclusions": [], "decisions": [], "dependencies": list(dependencies),
            "resources": list(resources), "risks": [], "evidence": []}


def publish_brief(cfg, conn, identifiers, actor=HUMAN):
    """A real published brief: strategy.create then strategy.approve."""
    from factory import strategy
    brief = strategy.create(cfg, conn, identifiers, actor, body=body())
    return strategy.approve(cfg, conn, brief["id"], actor)


def associate(conn, brief_id, issue_id, verdict_id):
    conn.execute("INSERT INTO brief_verdict(brief_id, issue_id, verdict_id) VALUES (?,?,?)",
                 (brief_id, issue_id, verdict_id))


def seed_dispatch(c, run_id, state="draft", brief_id=None, route=None, resources=("global:*",), pane="pane-1"):
    """Insert a dispatch (born draft) and reach `state` (draft|staged|executing) through legal transitions: claims
    pinned while draft, stage, then a reserved matching launch before executing (satisfies the real admission guards).
    """
    c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
              "VALUES (?,'draft','[{\"repo\":\"o/api\",\"trunk_sha\":\"s\"}]','x',?)", (run_id, SNAP))
    if brief_id is not None:
        c.execute("UPDATE dispatch SET brief_id=? WHERE run_id=?", (brief_id, run_id))
    if route:
        c.execute("UPDATE dispatch SET route=? WHERE run_id=?", (route, run_id))
    if state == "draft":
        return run_id
    set_claims(c, run_id, resources)  # pinned while draft
    c.execute("UPDATE dispatch SET state='staged', body_sha256='h', approved_by='u', last_actor='p' WHERE run_id=?",
              (run_id,))
    if state == "staged":
        return run_id
    c.execute("INSERT INTO dispatch_launch(run_id, pane_id, state, claimed_at) VALUES (?,?,?,?)",
              (run_id, pane, "reserved", SNAP))
    c.execute("UPDATE dispatch SET state='executing', executor_pane=?, executing_at=? WHERE run_id=?",
              (pane, SNAP, run_id))
    return run_id


def set_claims(c, run_id, resources):
    """Pin a draft dispatch's resource claims (guard: draft only)."""
    for r in sorted(set(resources)):
        c.execute("INSERT INTO dispatch_resource(run_id, resource) VALUES (?,?)", (run_id, r))


def seed_ticket(c, run_id, issue_id, ident, verdict_id, snap=SNAP):
    c.execute("INSERT INTO dispatch_ticket(run_id,issue_id,identifier,snapshot_updated_at,verdict_id) "
              "VALUES (?,?,?,?,?)", (run_id, issue_id, ident, snap, verdict_id))
