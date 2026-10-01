"""Execution slice behavioral regressions: hierarchical resource conflict, bounded capacity admission with a
single winner, brief-backed staging (pinned sources, staleness, consumed-once), the bounded propose queue with
blocked-first independence, uncertain-send safety, and the direct execute/resume guards.

The Briefs slice owns schema.sql v21 and the DB guard triggers; these tests build the contract-minimum tables on top
of the current schema so they run both before and after the schema lands (the helper is idempotent)."""
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import db, dispatch, prune, scheduler
from factory.config import Config, Context

SNAP = "2026-09-01T00:00:00Z"


def ensure_schema(c):
    """Idempotently add the v21 execution/brief tables the Execution slice consumes (schema owner's SQL matches)."""
    have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "work_brief" not in have:
        c.executescript("""
        CREATE TABLE work_brief (
          id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, parent_id INTEGER REFERENCES work_brief(id),
          state TEXT NOT NULL CHECK (state IN ('draft','approved','held')),
          body_json TEXT NOT NULL CHECK (json_valid(body_json)),
          sources_json TEXT NOT NULL CHECK (json_valid(sources_json)),
          created_at TEXT NOT NULL, created_by TEXT NOT NULL,
          approved_at TEXT, approved_by TEXT, amendment_reason TEXT);""")
    if "brief_id" not in {r[1] for r in c.execute("PRAGMA table_info(dispatch)")}:
        c.execute("ALTER TABLE dispatch ADD COLUMN brief_id INTEGER REFERENCES work_brief(id)")
    if "dispatch_resource" not in have:
        c.execute("CREATE TABLE dispatch_resource (run_id TEXT NOT NULL REFERENCES dispatch(run_id), "
                  "resource TEXT NOT NULL, PRIMARY KEY (run_id, resource));")
    if "execution_policy" not in have:
        c.executescript("CREATE TABLE execution_policy (id INTEGER PRIMARY KEY CHECK (id=1), "
                        "max_parallel INTEGER NOT NULL DEFAULT 2);"
                        "INSERT INTO execution_policy(id, max_parallel) VALUES (1, 2);")
    if "dispatch_launch" not in have:
        c.execute("CREATE TABLE dispatch_launch (run_id TEXT PRIMARY KEY REFERENCES dispatch(run_id), "
                  "pane_id TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('reserved','sent','uncertain')), "
                  "owner_pid INTEGER, claimed_at TEXT NOT NULL, sent_at TEXT, error TEXT);")


def seed_snapshot(c, issue_id, ident, snap=SNAP, state_type="unstarted", domain="API", in_scope=1, assignee=None):
    raw = json.dumps({"identifier": ident, "title": f"Fix {ident}", "url": f"https://linear.app/j/{ident}",
                      "state": {"name": "Todo", "type": "unstarted"}, "team": {"key": "FIN", "id": "team"},
                      "assignee": assignee, "priority": 2, "description": f"Domain: {domain}",
                      "labels": {"nodes": []}})
    c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,?,?,?)", (issue_id, ident, snap, snap, state_type, in_scope, raw))


def seed_verdict(c, issue_id, snap=SNAP, context="api", repo="o/api"):
    c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,context,repo,kind,reason,evidence_json,"
              "evidence_paths_json,created_at,created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (issue_id, snap, context, repo, "valid", "r", "[1]", "[]", db.now(), "t"))


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


def brief_dict(bid, identifiers, snap=SNAP, state="approved", resources=("global:*",)):
    return {"id": bid, "state": state,
            "body": {"title": f"Brief {bid}", "outcome": "done", "acceptance": ["a"], "scope": ["s"],
                     "resources": list(resources)},
            "sources": [{"identifier": i, "issue_id": f"i{i.split('-')[1]}", "snapshot_updated_at": snap,
                         "repo": "o/api", "context": "api", "route": "fx-api"} for i in identifiers]}


def seed_brief(c, bid, identifiers, snap=SNAP, state="approved", body_resources=("global:*",)):
    body = {"title": f"Brief {bid}", "outcome": "done", "acceptance": ["a"], "scope": ["s"],
            "resources": list(body_resources)}
    sources = [{"identifier": i, "issue_id": f"i{i.split('-')[1]}", "snapshot_updated_at": snap} for i in identifiers]
    c.execute("INSERT INTO work_brief(id, revision, state, body_json, sources_json, created_at, created_by, "
              "approved_at, approved_by) VALUES (?,1,?,?,?,?,?,?,?)",
              (bid, state, json.dumps(body), json.dumps(sources), SNAP, "u", SNAP, "u"))


def mkcfg(tmp):
    return Config(raw={"linear": {"lead": "lead@x", "team": {"FIN": {"review_state": "Ready for QA"}}}},
                  db=tmp / "t.db", mirrors=tmp, dispatches=tmp / "dispatches",
                  contexts=[Context("api", "o/api", domains=["API"], route="fx-api")], repos={}, witnesses={})


class ResourceConflict(unittest.TestCase):
    def test_conflict_rules(self):
        yes = [("global:*", "clickhouse:serving"), ("clickhouse:serving", "clickhouse:serving"),
               ("clickhouse:serving", "clickhouse:serving/ck_dev"),
               ("clickhouse:serving/ck_dev", "clickhouse:serving/ck_dev/master_profiles"),
               ("repo:Finks-ai/foo", "repo:Finks-ai/foo")]
        no = [("clickhouse:serving", "clickhouse:serving2"),
              ("clickhouse:serving/ck_dev", "clickhouse:serving/ck_dev2"),
              ("repo:Finks-ai/foo", "repo:Finks-ai/foo-bar"),
              ("repo:Finks-ai/foo", "route:fx-api"),
              ("stack:finks-infra/staging", "clickhouse:serving")]
        for a, b in yes:
            self.assertTrue(scheduler.resources_conflict(a, b), (a, b))
        for a, b in no:
            self.assertFalse(scheduler.resources_conflict(a, b), (a, b))


class Admission(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "t.db"
        self.c = db.connect(self.path)
        self.addCleanup(self.c.close)
        ensure_schema(self.c)

    def staged(self, run_id, resources, pane=None):
        seed_dispatch(self.c, run_id, "staged")
        scheduler.set_claims(self.c, run_id, resources)
        return run_id

    def test_capacity_bound_and_distinct_counting(self):
        self.c.execute("UPDATE execution_policy SET max_parallel=2")
        for run, res, pane in (("d1", {"repo:a", "route:fx-a"}, "p1"), ("d2", {"repo:b", "route:fx-b"}, "p2")):
            self.staged(run, res)
            self.assertIsNone(scheduler.reserve_if_free(self.c, run, pane, os.getpid()))
        self.staged("d3", {"repo:c", "route:fx-c"})
        self.assertIn("capacity full", scheduler.reserve_if_free(self.c, "d3", "p3", os.getpid()))
        # an executing dispatch plus its own sent launch still counts once
        self.c.execute("UPDATE dispatch SET state='executing', executing_at=? WHERE run_id='d1'", (SNAP,))
        self.assertEqual(scheduler.capacity_used(self.c), 2)

    def test_unknown_global_conflicts_with_everything(self):
        self.staged("d1", {"global:*"})
        self.assertIsNone(scheduler.reserve_if_free(self.c, "d1", "p1", os.getpid()))
        self.staged("d2", {"clickhouse:serving/ck_dev/master_profiles", "repo:b"})
        self.assertIn("global:* held by dispatch d1", scheduler.reserve_if_free(self.c, "d2", "p2", os.getpid()))

    def test_hierarchical_resource_conflict_blocks(self):
        self.staged("d1", {"clickhouse:serving", "repo:a"})
        self.assertIsNone(scheduler.reserve_if_free(self.c, "d1", "p1", os.getpid()))
        self.staged("d2", {"clickhouse:serving/ck_dev", "repo:b"})
        self.assertIn("resource conflict", scheduler.reserve_if_free(self.c, "d2", "p2", os.getpid()))

    def test_pane_exclusivity(self):
        self.staged("d1", {"repo:a", "route:fx-a"})
        self.assertIsNone(scheduler.reserve_if_free(self.c, "d1", "p1", os.getpid()))
        self.staged("d2", {"repo:b", "route:fx-b"})
        self.assertIn("pane p1 is reserved for dispatch d1", scheduler.reserve_if_free(self.c, "d2", "p1", os.getpid()))

    def test_terminal_frees_capacity_but_claims_held_until_archive(self):
        self.staged("d1", {"repo:a", "route:fx-a"})
        self.assertIsNone(scheduler.reserve_if_free(self.c, "d1", "p1", os.getpid()))
        self.c.execute("UPDATE dispatch SET state='executing', executing_at=? WHERE run_id='d1'", (SNAP,))
        self.assertEqual(scheduler.capacity_used(self.c), 1)
        scheduler.release_launch(self.c, "d1")  # terminal: done/reconciled free the slot
        self.assertEqual(scheduler.capacity_used(self.c), 0)
        # claims still block a later independent dispatch until archived
        self.staged("d2", {"repo:a", "route:fx-b"})
        self.assertIn("resource conflict", scheduler.reserve_if_free(self.c, "d2", "p2", os.getpid()))
        scheduler.release_claims(self.c, "d1")  # archive releases claims
        self.assertIsNone(scheduler.reserve_if_free(self.c, "d2", "p2", os.getpid()))

    def test_concurrent_reserve_single_winner(self):
        self.c.execute("UPDATE execution_policy SET max_parallel=1")
        self.staged("d1", {"repo:a", "route:fx-a"})
        self.staged("d2", {"repo:b", "route:fx-b"})
        c2 = db.connect(self.path)
        self.addCleanup(c2.close)
        out = []

        def worker(conn, run, pane):
            out.append((run, scheduler.reserve_if_free(conn, run, pane, os.getpid())))

        threads = [threading.Thread(target=worker, args=(conn, run, pane))
                   for conn, run, pane in ((self.c, "d1", "p1"), (c2, "d2", "p2"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [r for r in out if r[1] is None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.c.execute("SELECT count(*) FROM dispatch_launch").fetchone()[0], 1)


class StageBrief(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp = Path(tmp.name)
        self.c = db.connect(tmp / "t.db")
        self.addCleanup(self.c.close)
        ensure_schema(self.c)
        self.c.execute("INSERT INTO linear_project VALUES ('p','s','API','lead@x',?)", (SNAP,))
        self.c.execute("INSERT INTO repo_trunk VALUES ('o/api','main','deadbeef',?)", (SNAP,))
        seed_snapshot(self.c, "i1", "FIN-1")
        seed_verdict(self.c, "i1")
        self.cfg = mkcfg(tmp)

    def test_stage_brief_pins_brief_and_resources(self):
        seed_brief(self.c, 1, ["FIN-1"])
        with mock.patch.object(dispatch, "_brief", return_value=brief_dict(1, ["FIN-1"])):
            res = dispatch.stage_brief(self.cfg, self.c, 1, "user")
        self.assertEqual(res["brief_id"], 1)
        row = self.c.execute("SELECT brief_id, state FROM dispatch WHERE run_id=?", (res["run_id"],)).fetchone()
        self.assertEqual((row["brief_id"], row["state"]), (1, "draft"))
        claims = scheduler.claims(self.c, res["run_id"])
        self.assertEqual(claims, {"global:*", "repo:o/api", "route:fx-api"})

    def test_stage_brief_refuses_non_approved(self):
        seed_brief(self.c, 1, ["FIN-1"], state="draft")
        with mock.patch.object(dispatch, "_brief", return_value=brief_dict(1, ["FIN-1"], state="draft")):
            with self.assertRaisesRegex(dispatch.StageError, "not approved"):
                dispatch.stage_brief(self.cfg, self.c, 1, "user")

    def test_stage_brief_consumed_once(self):
        seed_brief(self.c, 1, ["FIN-1"])
        with mock.patch.object(dispatch, "_brief", return_value=brief_dict(1, ["FIN-1"])):
            dispatch.stage_brief(self.cfg, self.c, 1, "user")
            with self.assertRaisesRegex(dispatch.StageError, "already dispatched"):
                dispatch.stage_brief(self.cfg, self.c, 1, "user")

    def test_stage_brief_refuses_source_changed_since_capture(self):
        seed_brief(self.c, 1, ["FIN-1"])
        # the source moved after the brief captured it: a new snapshot version exists
        self.c.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1',?,?,?,?,?)",
                       ("2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z", "unstarted", 1,
                        json.dumps({"identifier": "FIN-1", "title": "x", "state": {"name": "Todo"},
                                    "team": {"key": "FIN"}, "assignee": None, "labels": {"nodes": []},
                                    "description": "Domain: API"})))
        with mock.patch.object(dispatch, "_brief", return_value=brief_dict(1, ["FIN-1"])):
            with self.assertRaisesRegex(dispatch.StageError, "changed since the brief was captured"):
                dispatch.stage_brief(self.cfg, self.c, 1, "user")

    def test_stage_identifiers_resolve_exact_approved_brief(self):
        seed_brief(self.c, 1, ["FIN-1"])
        with mock.patch.object(dispatch, "_brief", return_value=brief_dict(1, ["FIN-1"])):
            res = dispatch.stage(self.cfg, self.c, ["FIN-1"], "user")
        self.assertEqual(res["brief_id"], 1)

    def test_stage_without_matching_brief_refuses(self):
        with self.assertRaisesRegex(dispatch.StageError, "no approved brief matches exactly"):
            dispatch.stage(self.cfg, self.c, ["FIN-1"], "user")


class Propose(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.c = db.connect(Path(tmp.name) / "t.db")
        self.addCleanup(self.c.close)
        ensure_schema(self.c)

    def test_handoff_does_not_starve_independent_second(self):
        seed_dispatch(self.c, "d1", "staged")
        seed_dispatch(self.c, "d2", "staged")

        def fake_handoff(cfg, conn, run_id):
            if run_id == "d1":
                raise dispatch.StageError("resource conflict: repo:a held by another dispatch")
            return {"run_id": run_id}

        with mock.patch.object(dispatch, "handoff", side_effect=fake_handoff), \
                mock.patch.object(dispatch, "_ready", return_value=[]):
            res = dispatch.propose(SimpleNamespace(raw={}), self.c)
        self.assertEqual([(h["run_id"], "blocked" in h) for h in res["handoffs"]],
                         [("d1", True), ("d2", False)])

    def test_prepare_skips_blocked_brief_and_prepares_next(self):
        ready = [{"id": 1, "ready": False, "blockers": ["no evidence"]},
                 {"id": 2, "ready": True, "blockers": []}]

        def fake_stage(cfg, conn, brief_id, actor):
            return {"run_id": f"r{brief_id}", "brief_id": brief_id}

        with mock.patch.object(dispatch, "_ready", return_value=ready), \
                mock.patch.object(dispatch, "stage_brief", side_effect=fake_stage), \
                mock.patch.object(dispatch, "handoff", return_value={}):
            res = dispatch.propose(SimpleNamespace(raw={}), self.c)
        self.assertEqual(res["prepared"], [{"brief_id": 2, "run_id": "r2"}])
        self.assertEqual(res["blocked"], [{"brief_id": 1, "blockers": ["no evidence"]}])

    def test_prepare_respects_queue_cap(self):
        seed_dispatch(self.c, "d1", "draft")
        seed_dispatch(self.c, "d2", "draft")
        seed_dispatch(self.c, "d3", "staged")  # 3 nonexecuting already: nothing more is prepared
        ready = [{"id": 9, "ready": True, "blockers": []}]
        with mock.patch.object(dispatch, "_ready", return_value=ready), \
                mock.patch.object(dispatch, "stage_brief") as stage, \
                mock.patch.object(dispatch, "handoff", return_value={}):
            dispatch.propose(SimpleNamespace(raw={"executor": {"queue_cap": 3}}), self.c)
        stage.assert_not_called()


class UncertainSend(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "t.db"
        self.c = db.connect(self.path)
        self.addCleanup(self.c.close)
        ensure_schema(self.c)
        seed_dispatch(self.c, "d1", "staged")
        scheduler.set_claims(self.c, "d1", {"repo:a", "route:fx-a"})
        self.cfg = SimpleNamespace(raw={})
        self.pane = {"pane_id": "w1:p1", "agent": "omp", "agent_status": "idle"}

    def test_send_failure_marks_uncertain_and_is_not_replayed(self):
        with mock.patch.object(dispatch, "_target", return_value=(self.pane, "factory-primary")), \
                mock.patch.object(dispatch, "_send", side_effect=dispatch.StageError("pane unavailable")):
            with self.assertRaisesRegex(dispatch.StageError, "uncertain"):
                dispatch.handoff(self.cfg, self.c, "d1")
        self.assertEqual(scheduler.launch(self.c, "d1")["state"], "uncertain")
        # a second handoff never re-sends after an uncertain send
        with mock.patch.object(dispatch, "_target", return_value=(self.pane, "factory-primary")), \
                mock.patch.object(dispatch, "_send") as send, \
                self.assertRaisesRegex(dispatch.StageError, "uncertain send"):
            dispatch.handoff(self.cfg, self.c, "d1")
        send.assert_not_called()

    def test_release_unsent_recovers_an_uncertain_launch(self):
        self.c.execute("INSERT INTO dispatch_launch(run_id,pane_id,state,claimed_at) VALUES ('d1','w1:p1','uncertain',?)",
                       (SNAP,))
        panes = {"result": {"panes": [{"pane_id": "w1:p1", "agent": "omp", "agent_status": "idle"}]}}
        with mock.patch.object(dispatch, "_herdr", return_value=panes):
            res = dispatch.release_unsent(self.cfg, self.c, "d1", "user", "never reached the pane")
        self.assertEqual(res["released"], "uncertain")
        self.assertIsNone(scheduler.launch(self.c, "d1"))

    def test_release_unsent_guards(self):
        self.c.execute("INSERT INTO dispatch_launch(run_id,pane_id,state,claimed_at) VALUES ('d1','w1:p1','sent',?)",
                       (SNAP,))
        with self.assertRaisesRegex(dispatch.StageError, "already sent"):
            dispatch.release_unsent(self.cfg, self.c, "d1", "user", "reason")
        self.c.execute("UPDATE dispatch_launch SET state='uncertain' WHERE run_id='d1'")
        with self.assertRaisesRegex(dispatch.StageError, "say why"):
            dispatch.release_unsent(self.cfg, self.c, "d1", "user", "  ")
        busy = {"result": {"panes": [{"pane_id": "w1:p1", "agent": "omp", "agent_status": "running"}]}}
        with mock.patch.object(dispatch, "_herdr", return_value=busy), \
                self.assertRaisesRegex(dispatch.StageError, "not releasing a live executor"):
            dispatch.release_unsent(self.cfg, self.c, "d1", "user", "reason")


class ExecuteGuard(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.c = db.connect(Path(tmp.name) / "t.db")
        self.addCleanup(self.c.close)
        ensure_schema(self.c)

    def test_staged_to_executing_requires_its_own_launch(self):
        body = b"# Dispatch d1\n"
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at,route) "
                       "VALUES ('d1','draft','[]','x',?,'fx-api')", (SNAP,))
        self.c.execute("UPDATE dispatch SET state='staged', body_sha256=?, approved_by='u', last_actor='p' "
                       "WHERE run_id='d1'", (hashlib.sha256(body).hexdigest(),))
        scheduler.set_claims(self.c, "d1", {"repo:a", "route:fx-a"})
        dispatches = Path(tempfile.mkdtemp())
        (dispatches / "d1").mkdir(parents=True)
        (dispatches / "d1" / "dispatch.md").write_bytes(body)
        with mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w", "HERDR_PANE_ID": "w:p1"}), \
                mock.patch.object(dispatch, "_lead_pane_id", return_value="w:p1"):
            with self.assertRaisesRegex(dispatch.StageError, "no launch reservation"):
                dispatch.execute(SimpleNamespace(raw={}, dispatches=dispatches), self.c, "d1", "lead")

    def test_resume_refuses_another_dispatchs_pane(self):
        seed_dispatch(self.c, "d1", "executing")
        seed_dispatch(self.c, "d2", "staged")
        self.c.execute("INSERT INTO dispatch_launch(run_id,pane_id,state,claimed_at) VALUES ('d2','w1:p1','sent',?)",
                       (SNAP,))
        pane = {"pane_id": "w1:p1", "agent": "omp", "agent_status": "idle"}
        with mock.patch.object(dispatch, "_target", return_value=(pane, "factory-primary")), \
                self.assertRaisesRegex(dispatch.StageError, "reserved for dispatch d2"):
            dispatch.resume(SimpleNamespace(raw={}), self.c, "d1")


class PruneBacklogBrief(unittest.TestCase):
    def test_approved_backlog_brief_enters_prune_with_narrative(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        c = db.connect(Path(tmp.name) / "t.db")
        self.addCleanup(c.close)
        ensure_schema(c)
        c.execute("INSERT INTO linear_project VALUES ('p','s','API','lead@x',?)", (SNAP,))
        c.execute("INSERT INTO repo_trunk VALUES ('o/api','main','deadbeef',?)", (SNAP,))
        seed_snapshot(c, "i1", "FIN-1", state_type="backlog", in_scope=0)  # Backlog source
        seed_brief(c, 1, ["FIN-1"])
        cfg = mkcfg(Path(tmp.name))
        with mock.patch.object(prune, "_brief_sources", return_value={"FIN-1": {"brief_id": 1, "narrative": "# Brief 1"}}):
            got = prune.gate(cfg, c)
        ids = [t["identifier"] for t in got["context"]["tickets"]]
        self.assertIn("FIN-1", ids)
        item = next(t for t in got["context"]["tickets"] if t["identifier"] == "FIN-1")
        self.assertEqual(item["brief"], "# Brief 1")
        self.assertEqual(item["why"], "new")


if __name__ == "__main__":
    unittest.main()
