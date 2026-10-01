"""Shared v21 execution fixtures for the Execution slice's tests.

The Briefs slice owns schema.sql/db.py v21 (work_brief, scheduling guards, launch/resource tables). This module
reproduces that exact DDL (plus the minimal ``brief_verdict`` association the parent required) so the Execution
tests run both standalone (on the v20 schema still in this worktree) and after the Briefs schema lands (where
``ensure_schema`` is a no-op). Do not edit; the schema owner's copy is authoritative.
"""
import json
from pathlib import Path

from factory import db

SNAP = "2026-09-01T00:00:00Z"

# Faithful copy of schema v21 execution objects + the brief_verdict association.
V21_DDL = """
CREATE TABLE work_brief (
  id INTEGER PRIMARY KEY,
  revision INTEGER NOT NULL CHECK (revision >= 1),
  parent_id INTEGER REFERENCES work_brief(id),
  state TEXT NOT NULL CHECK (state IN ('draft', 'approved', 'held')),
  body_json TEXT NOT NULL CHECK (json_valid(body_json)),
  sources_json TEXT NOT NULL CHECK (json_valid(sources_json) AND json_array_length(sources_json) > 0),
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  approved_at TEXT, approved_by TEXT, amendment_reason TEXT, hold_reason TEXT,
  CHECK ((approved_at IS NULL) = (approved_by IS NULL)),
  CHECK ((state IN ('approved', 'held')) = (approved_at IS NOT NULL))
);
CREATE INDEX work_brief_parent ON work_brief(parent_id) WHERE parent_id IS NOT NULL;
CREATE TRIGGER work_brief_root_revision BEFORE INSERT ON work_brief
WHEN NEW.parent_id IS NULL AND NEW.revision <> 1
BEGIN SELECT RAISE(ABORT, 'a root work brief is revision 1'); END;
CREATE TRIGGER work_brief_revision BEFORE INSERT ON work_brief
WHEN NEW.parent_id IS NOT NULL AND NEW.revision <> (SELECT revision + 1 FROM work_brief WHERE id = NEW.parent_id)
BEGIN SELECT RAISE(ABORT, 'an amendment increments its parent revision'); END;
CREATE TRIGGER work_brief_edges BEFORE UPDATE OF state ON work_brief
WHEN NEW.state IS NOT OLD.state AND (OLD.state, NEW.state) NOT IN (VALUES
  ('draft', 'approved'), ('approved', 'held'), ('held', 'approved'))
BEGIN SELECT RAISE(ABORT, 'illegal work_brief state transition'); END;
CREATE TRIGGER work_brief_frozen BEFORE UPDATE ON work_brief
WHEN OLD.state IN ('approved', 'held') AND (NEW.body_json IS NOT OLD.body_json
  OR NEW.sources_json IS NOT OLD.sources_json OR NEW.parent_id IS NOT OLD.parent_id
  OR NEW.revision IS NOT OLD.revision OR NEW.created_at IS NOT OLD.created_at
  OR NEW.created_by IS NOT OLD.created_by OR NEW.amendment_reason IS NOT OLD.amendment_reason)
BEGIN SELECT RAISE(ABORT, 'a published work brief is immutable; amend it to create a new revision'); END;
CREATE TRIGGER work_brief_no_delete BEFORE DELETE ON work_brief
BEGIN SELECT RAISE(ABORT, 'work briefs are never deleted'); END;

CREATE TABLE work_brief_hold (
  id INTEGER PRIMARY KEY, brief_id INTEGER NOT NULL REFERENCES work_brief(id),
  action TEXT NOT NULL CHECK (action IN ('hold', 'unhold')), reason TEXT, actor TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TRIGGER work_brief_hold_append_only_u BEFORE UPDATE ON work_brief_hold
BEGIN SELECT RAISE(ABORT, 'hold audit is append-only'); END;
CREATE TRIGGER work_brief_hold_append_only_d BEFORE DELETE ON work_brief_hold
BEGIN SELECT RAISE(ABORT, 'hold audit is append-only'); END;

ALTER TABLE dispatch ADD COLUMN brief_id INTEGER REFERENCES work_brief(id);
CREATE UNIQUE INDEX dispatch_one_brief ON dispatch(brief_id) WHERE brief_id IS NOT NULL;

CREATE TABLE dispatch_resource (
  run_id TEXT NOT NULL REFERENCES dispatch(run_id), resource TEXT NOT NULL, PRIMARY KEY (run_id, resource)
);
INSERT INTO dispatch_resource(run_id, resource)
  SELECT run_id, 'global:*' FROM dispatch WHERE state IN ('staged', 'executing');
CREATE TRIGGER dispatch_resource_draft_only_i BEFORE INSERT ON dispatch_resource
WHEN (SELECT state FROM dispatch WHERE run_id = NEW.run_id) IS NOT 'draft'
BEGIN SELECT RAISE(ABORT, 'dispatch resources are pinned while the dispatch is a draft'); END;
CREATE TRIGGER dispatch_resource_no_update BEFORE UPDATE ON dispatch_resource
BEGIN SELECT RAISE(ABORT, 'dispatch resources are immutable'); END;
CREATE TRIGGER dispatch_resource_held_to_archive BEFORE DELETE ON dispatch_resource
WHEN (SELECT state FROM dispatch WHERE run_id = OLD.run_id) IS NOT 'archived'
BEGIN SELECT RAISE(ABORT, 'dispatch resources are held until the dispatch is archived'); END;

CREATE TABLE execution_policy (
  id INTEGER PRIMARY KEY CHECK (id = 1), max_parallel INTEGER NOT NULL DEFAULT 2 CHECK (max_parallel >= 1)
);
INSERT INTO execution_policy(id, max_parallel) VALUES (1, 2);

CREATE TABLE dispatch_launch (
  run_id TEXT PRIMARY KEY REFERENCES dispatch(run_id),
  pane_id TEXT NOT NULL CHECK (length(trim(pane_id)) > 0),
  state TEXT NOT NULL CHECK (state IN ('reserved', 'sent', 'uncertain')),
  owner_pid INTEGER, claimed_at TEXT, sent_at TEXT, error TEXT
);
CREATE TRIGGER launch_reserve_state BEFORE INSERT ON dispatch_launch
WHEN (SELECT state FROM dispatch WHERE run_id = NEW.run_id) IS NOT 'staged'
BEGIN SELECT RAISE(ABORT, 'a launch is reserved for a staged dispatch'); END;
CREATE TRIGGER launch_edges BEFORE UPDATE OF state ON dispatch_launch
WHEN NEW.state IS NOT OLD.state AND (OLD.state, NEW.state) NOT IN (VALUES
  ('reserved', 'sent'), ('reserved', 'uncertain'), ('uncertain', 'sent'), ('uncertain', 'reserved'))
BEGIN SELECT RAISE(ABORT, 'illegal launch state transition'); END;
CREATE TRIGGER launch_release_guard BEFORE DELETE ON dispatch_launch
WHEN OLD.state IN ('sent', 'uncertain')
 AND (SELECT state FROM dispatch WHERE run_id = OLD.run_id) NOT IN ('done', 'reconciled', 'archived')
BEGIN SELECT RAISE(ABORT, 'a sent/uncertain launch is released only once its dispatch is terminal'); END;

CREATE VIEW launch_active AS
  SELECT run_id, route, executor_pane AS pane_id FROM dispatch WHERE state = 'executing'
  UNION ALL
  SELECT l.run_id, d.route, l.pane_id FROM dispatch_launch l JOIN dispatch d ON d.run_id = l.run_id
   WHERE l.state IN ('reserved', 'sent', 'uncertain') AND d.state NOT IN ('done', 'reconciled', 'archived');

CREATE VIEW resource_holders AS
  SELECT run_id FROM dispatch WHERE state IN ('executing', 'done', 'reconciled')
  UNION
  SELECT l.run_id FROM dispatch_launch l JOIN dispatch d ON d.run_id = l.run_id
   WHERE l.state IN ('reserved', 'sent', 'uncertain') AND d.state NOT IN ('done', 'reconciled', 'archived');

CREATE VIEW resource_conflicts AS
SELECT c.run_id AS a, o.run_id AS b
FROM dispatch_resource c JOIN dispatch_resource o ON o.run_id <> c.run_id
WHERE c.resource = o.resource
   OR substr(c.resource, 1, instr(c.resource, ':') - 1) = 'global'
   OR substr(o.resource, 1, instr(o.resource, ':') - 1) = 'global'
   OR (substr(c.resource, 1, instr(c.resource, ':') - 1) = substr(o.resource, 1, instr(o.resource, ':') - 1)
       AND (substr(c.resource, instr(c.resource, ':') + 1) = substr(o.resource, instr(o.resource, ':') + 1)
            OR instr(substr(c.resource, instr(c.resource, ':') + 1), substr(o.resource, instr(o.resource, ':') + 1) || '/') = 1
            OR instr(substr(o.resource, instr(o.resource, ':') + 1), substr(c.resource, instr(c.resource, ':') + 1) || '/') = 1));

CREATE TRIGGER launch_reserve_guard BEFORE INSERT ON dispatch_launch
WHEN (SELECT count(DISTINCT run_id) FROM launch_active) + 1 > (SELECT max_parallel FROM execution_policy WHERE id = 1)
  OR EXISTS (SELECT 1 FROM launch_active WHERE pane_id = NEW.pane_id)
  OR ((SELECT route FROM dispatch WHERE run_id = NEW.run_id) IS NOT NULL
      AND EXISTS (SELECT 1 FROM launch_active WHERE route = (SELECT route FROM dispatch WHERE run_id = NEW.run_id)))
  OR EXISTS (SELECT 1 FROM resource_conflicts rc WHERE rc.a = NEW.run_id AND rc.b IN (SELECT run_id FROM resource_holders))
BEGIN SELECT RAISE(ABORT, 'launch conflicts with capacity, pane, route or a held resource'); END;

CREATE TRIGGER dispatch_execute_guard BEFORE UPDATE OF state ON dispatch
WHEN OLD.state = 'staged' AND NEW.state = 'executing' AND (
  (SELECT count(DISTINCT run_id) FROM launch_active WHERE run_id <> NEW.run_id) + 1
      > (SELECT max_parallel FROM execution_policy WHERE id = 1)
  OR EXISTS (SELECT 1 FROM launch_active WHERE pane_id = NEW.executor_pane AND run_id <> NEW.run_id)
  OR (NEW.route IS NOT NULL AND EXISTS (SELECT 1 FROM launch_active WHERE route = NEW.route AND run_id <> NEW.run_id))
  OR EXISTS (SELECT 1 FROM resource_conflicts rc WHERE rc.a = NEW.run_id
             AND rc.b IN (SELECT run_id FROM resource_holders WHERE run_id <> NEW.run_id)))
BEGIN SELECT RAISE(ABORT, 'execute conflicts with capacity, pane, route or a held resource'); END;

CREATE TRIGGER dispatch_execute_launch BEFORE UPDATE OF state ON dispatch
WHEN OLD.state = 'staged' AND NEW.state = 'executing' AND NEW.brief_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM dispatch_launch WHERE run_id = NEW.run_id
                 AND state IN ('reserved', 'sent') AND pane_id = NEW.executor_pane)
BEGIN SELECT RAISE(ABORT, 'a brief-backed dispatch executes only from its reserved/sent launch pane'); END;

DROP INDEX one_executing;
DROP TRIGGER dispatch_frozen;
CREATE TRIGGER dispatch_frozen BEFORE UPDATE ON dispatch
WHEN OLD.state <> 'draft' AND (NEW.body_sha256 IS NOT OLD.body_sha256 OR NEW.repos_json IS NOT OLD.repos_json
  OR NEW.run_id IS NOT OLD.run_id OR NEW.created_at IS NOT OLD.created_at OR NEW.route IS NOT OLD.route
  OR NEW.brief_id IS NOT OLD.brief_id)
BEGIN SELECT RAISE(ABORT, 'dispatch is immutable once staged'); END;

CREATE TABLE brief_verdict (
  brief_id INTEGER NOT NULL REFERENCES work_brief(id),
  issue_id TEXT NOT NULL,
  verdict_id INTEGER NOT NULL REFERENCES verdict(id),
  PRIMARY KEY (brief_id, issue_id)
);
CREATE TRIGGER brief_verdict_consistent BEFORE INSERT ON brief_verdict
WHEN (SELECT issue_id FROM verdict WHERE id = NEW.verdict_id) IS NOT NEW.issue_id
BEGIN SELECT RAISE(ABORT, 'brief_verdict verdict must be for its issue'); END;
"""


def ensure_schema(c):
    """Idempotently add the v21 execution schema + brief_verdict on top of this worktree's v20 schema."""
    have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "work_brief" not in have:
        c.executescript(V21_DDL)
    elif "brief_verdict" not in have:
        c.executescript("""
CREATE TABLE brief_verdict (
  brief_id INTEGER NOT NULL REFERENCES work_brief(id),
  issue_id TEXT NOT NULL,
  verdict_id INTEGER NOT NULL REFERENCES verdict(id),
  PRIMARY KEY (brief_id, issue_id)
);
CREATE TRIGGER brief_verdict_consistent BEFORE INSERT ON brief_verdict
WHEN (SELECT issue_id FROM verdict WHERE id = NEW.verdict_id) IS NOT NEW.issue_id
BEGIN SELECT RAISE(ABORT, 'brief_verdict verdict must be for its issue'); END;
""")
    c.execute("PRAGMA user_version=21")


def seed_snapshot(c, issue_id, ident, snap=SNAP, state_type="unstarted", domain="API", in_scope=1, assignee=None):
    raw = json.dumps({"identifier": ident, "title": f"Fix {ident}", "url": f"https://linear.app/j/{ident}",
                      "state": {"name": "Todo", "type": "unstarted"}, "team": {"key": "FIN", "id": "team"},
                      "assignee": assignee, "priority": 2, "description": f"Domain: {domain}",
                      "labels": {"nodes": []}})
    c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,?,?,?)", (issue_id, ident, snap, snap, state_type, in_scope, raw))


def seed_verdict(c, issue_id, snap=SNAP, context="api", repo="o/api"):
    cur = c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,context,repo,kind,reason,evidence_json,"
                    "evidence_paths_json,created_at,created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (issue_id, snap, context, repo, "valid", "r", "[1]", "[]", db.now(), "t"))
    return cur.lastrowid


def seed_dispatch(c, run_id, state="draft", brief_id=None, route=None):
    c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
              "VALUES (?,'draft','[{\"repo\":\"o/api\",\"trunk_sha\":\"s\"}]','x',?)", (run_id, SNAP))
    if brief_id is not None:
        c.execute("UPDATE dispatch SET brief_id=? WHERE run_id=?", (brief_id, run_id))
    if route:
        c.execute("UPDATE dispatch SET route=? WHERE run_id=?", (route, run_id))
    if state != "draft":
        c.execute("UPDATE dispatch SET state='staged', body_sha256='h', approved_by='u', last_actor='p' WHERE run_id=?",
                  (run_id,))
    if state == "executing":
        c.execute("UPDATE dispatch SET state='executing', executing_at=?, executor_pane='pane1', last_actor='fm' "
                  "WHERE run_id=?", (SNAP, run_id))


def seed_brief(c, bid, identifiers, snap=SNAP, state="approved", body_resources=("global:*",), parent_id=None,
               revision=None):
    body = {"title": f"Brief {bid}", "outcome": "done", "acceptance": ["a"], "scope": ["s"],
            "exclusions": [], "decisions": [], "dependencies": [], "resources": list(body_resources),
            "risks": [], "evidence": []}
    sources = [{"identifier": i, "issue_id": f"i{i.split('-')[1]}", "snapshot_updated_at": snap} for i in identifiers]
    revision = revision or (1 if parent_id is None else None)
    c.execute("INSERT INTO work_brief(id, revision, parent_id, state, body_json, sources_json, created_at, created_by, "
              "approved_at, approved_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (bid, revision, parent_id, state, json.dumps(body), json.dumps(sources), SNAP, "u",
               SNAP if state in ("approved", "held") else None,
               "u" if state in ("approved", "held") else None))


def seed_association(c, brief_id, issue_id, verdict_id):
    c.execute("INSERT INTO brief_verdict(brief_id, issue_id, verdict_id) VALUES (?,?,?)",
              (brief_id, issue_id, verdict_id))


def brief_dict(bid, identifiers, snap=SNAP, state="approved", resources=("global:*",)):
    return {"id": bid, "state": state,
            "body": {"title": f"Brief {bid}", "outcome": "done", "acceptance": ["a"], "scope": ["s"],
                     "exclusions": [], "decisions": [], "dependencies": [], "resources": list(resources),
                     "risks": [], "evidence": []},
            "sources": [{"identifier": i, "issue_id": f"i{i.split('-')[1]}", "snapshot_updated_at": snap,
                         "repo": "o/api", "context": "api", "route": "fx-api"} for i in identifiers]}


def mkcfg(tmp: Path):
    from factory.config import Config, Context
    return Config(raw={"linear": {"lead": "lead@x", "team": {"FIN": {"review_state": "Ready for QA"}}}},
                  db=tmp / "t.db", mirrors=tmp, dispatches=tmp / "dispatches",
                  contexts=[Context("api", "o/api", domains=["API"], route="fx-api")], repos={}, witnesses={})
