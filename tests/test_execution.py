"""Execution slice behavioral regressions: hierarchical resource conflict, bounded capacity admission with a single
winner, exact-version brief verification (brief_verdict), the bounded propose queue with blocked-first independence,
uncertain-send safety, and the direct execute/resume pane guards."""
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import _v21  # noqa: E402

from factory import db, dispatch, prune, scheduler

SNAP = _v21.SNAP


class ResourceConflict(unittest.TestCase):
    def test_conflict_rules(self):
        yes = [("global:*", "clickhouse:serving"), ("clickhouse:serving", "clickhouse:serving"),
               ("clickhouse:serving", "clickhouse:serving/ck_dev"),
               ("clickhouse:serving/ck_dev", "clickhouse:serving/ck_dev/master_profiles"),
               ("repo:Finks-ai/foo", "repo:Finks-ai/foo"), ("global:anything", "repo:x")]
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
        _v21.ensure_schema(self.c)

    def staged(self, run_id, resources, route=None):
        _v21.seed_dispatch(self.c, run_id, "draft", route=route)
        scheduler.set_claims(self.c, run_id, resources)
        self.c.execute("UPDATE dispatch SET state='staged', body_sha256='h', approved_by='u', last_actor='p' "
                       "WHERE run_id=?", (run_id,))
        return run_id

    def test_capacity_bound_and_distinct_counting(self):
        self.c.execute("UPDATE execution_policy SET max_parallel=2")
        for run, res, pane in (("d1", {"repo:a", "route:fx-a"}, "p1"), ("d2", {"repo:b", "route:fx-b"}, "p2")):
            self.staged(run, res)
            self.assertIsNone(scheduler.reserve_if_free(self.c, run, pane, os.getpid()))
        self.staged("d3", {"repo:c", "route:fx-c"})
        self.assertIn("capacity full", scheduler.reserve_if_free(self.c, "d3", "p3", os.getpid()))
        self.c.execute("UPDATE dispatch SET state='executing', executing_at=? WHERE run_id='d1'", (SNAP,))
        self.assertEqual(scheduler.capacity_used(self.c), 2)  # executing + own sent launch counts once

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
        self.staged("d2", {"repo:a", "route:fx-b"})  # claims still conflict until archive
        self.assertIn("resource conflict", scheduler.reserve_if_free(self.c, "d2", "p2", os.getpid()))

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
        self.assertEqual(len([r for r in out if r[1] is None]), 1)
        self.assertEqual(self.c.execute("SELECT count(*) FROM dispatch_launch").fetchone()[0], 1)


class StageBrief(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp = Path(tmp.name)
        self.c = db.connect(tmp / "t.db")
        self.addCleanup(self.c.close)
        _v21.ensure_schema(self.c)
        self.c.execute("INSERT INTO linear_project VALUES ('p','s','API','lead@x',?)", (SNAP,))
        self.c.execute("INSERT INTO repo_trunk VALUES ('o/api','main','deadbeef',?)", (SNAP,))
        _v21.seed_snapshot(self.c, "i1", "FIN-1")
        self.verdict = _v21.seed_verdict(self.c, "i1")
        self.cfg = _v21.mkcfg(tmp)

    def test_stage_brief_requires_exact_association(self):
        _v21.seed_brief(self.c, 1, ["FIN-1"])  # approved but never verified for this exact version
        with mock.patch.object(dispatch, "_brief", return_value=_v21.brief_dict(1, ["FIN-1"])):
            with self.assertRaisesRegex(dispatch.StageError, "no recorded verification"):
                dispatch.stage_brief(self.cfg, self.c, 1, "user")

    def test_stage_brief_pins_association_and_resources(self):
        _v21.seed_brief(self.c, 1, ["FIN-1"])
        _v21.seed_association(self.c, 1, "i1", self.verdict)
        with mock.patch.object(dispatch, "_brief", return_value=_v21.brief_dict(1, ["FIN-1"])):
            res = dispatch.stage_brief(self.cfg, self.c, 1, "user")
        self.assertEqual(res["brief_id"], 1)
        row = self.c.execute("SELECT brief_id, state FROM dispatch WHERE run_id=?", (res["run_id"],)).fetchone()
        self.assertEqual((row["brief_id"], row["state"]), (1, "draft"))
        self.assertEqual(scheduler.claims(self.c, res["run_id"]), {"global:*", "repo:o/api", "route:fx-api"})

    def test_new_amendment_needs_new_verification(self):
        _v21.seed_brief(self.c, 1, ["FIN-1"])
        _v21.seed_association(self.c, 1, "i1", self.verdict)  # v1 verified
        _v21.seed_brief(self.c, 2, ["FIN-1"], parent_id=1, revision=2)  # approved amendment, unverified
        with mock.patch.object(dispatch, "_brief", return_value=_v21.brief_dict(2, ["FIN-1"])):
            with self.assertRaisesRegex(dispatch.StageError, "no recorded verification"):
                dispatch.stage_brief(self.cfg, self.c, 2, "user")  # source verdict is fresh, but this version is not
        with mock.patch.object(dispatch, "_brief", return_value=_v21.brief_dict(1, ["FIN-1"])):
            with self.assertRaisesRegex(dispatch.StageError, "superseded by a newer revision"):
                dispatch.stage_brief(self.cfg, self.c, 1, "user")  # old version superseded by approved child

    def test_stage_brief_refuses_source_changed_since_capture(self):
        _v21.seed_brief(self.c, 1, ["FIN-1"])
        _v21.seed_association(self.c, 1, "i1", self.verdict)
        self.c.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1',?,?,?,?,?)",
                       ("2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z", "unstarted", 1,
                        json.dumps({"identifier": "FIN-1", "title": "x", "state": {"name": "Todo"},
                                    "team": {"key": "FIN"}, "assignee": None, "labels": {"nodes": []},
                                    "description": "Domain: API"})))
        with mock.patch.object(dispatch, "_brief", return_value=_v21.brief_dict(1, ["FIN-1"])):
            with self.assertRaisesRegex(dispatch.StageError, "changed since the brief was captured"):
                dispatch.stage_brief(self.cfg, self.c, 1, "user")

    def test_stage_explicit_brief_rejects_non_approved(self):
        _v21.seed_brief(self.c, 1, ["FIN-1"], state="draft")
        with mock.patch.object(dispatch, "_brief", return_value=_v21.brief_dict(1, ["FIN-1"], state="draft")):
            with self.assertRaisesRegex(dispatch.StageError, "not approved"):
                dispatch.stage(self.cfg, self.c, ["FIN-1"], "user", brief_id=1)  # no bypass via explicit brief id

    def test_stage_brief_refuses_held(self):
        _v21.seed_brief(self.c, 1, ["FIN-1"], state="held")
        with mock.patch.object(dispatch, "_brief", return_value=_v21.brief_dict(1, ["FIN-1"], state="held")):
            with self.assertRaisesRegex(dispatch.StageError, "not approved"):
                dispatch.stage_brief(self.cfg, self.c, 1, "user")

    def test_stage_identifiers_resolve_exact_approved_brief(self):
        _v21.seed_brief(self.c, 1, ["FIN-1"])
        _v21.seed_association(self.c, 1, "i1", self.verdict)
        with mock.patch.object(dispatch, "_brief", return_value=_v21.brief_dict(1, ["FIN-1"])):
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
        _v21.ensure_schema(self.c)

    def test_handoff_does_not_starve_independent_second(self):
        _v21.seed_dispatch(self.c, "d1", "staged")
        _v21.seed_dispatch(self.c, "d2", "staged")

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
        _v21.seed_dispatch(self.c, "d1", "draft")
        _v21.seed_dispatch(self.c, "d2", "draft")
        _v21.seed_dispatch(self.c, "d3", "staged")  # 3 nonexecuting already: nothing more prepared
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
        _v21.ensure_schema(self.c)
        _v21.seed_dispatch(self.c, "d1", "draft")
        scheduler.set_claims(self.c, "d1", {"repo:a", "route:fx-a"})
        self.c.execute("UPDATE dispatch SET state='staged', body_sha256='h', approved_by='u', last_actor='p' "
                       "WHERE run_id='d1'")
        self.cfg = SimpleNamespace(raw={})
        self.pane = {"pane_id": "w1:p1", "agent": "omp", "agent_status": "idle"}

    def test_send_failure_marks_uncertain_and_is_not_replayed(self):
        with mock.patch.object(dispatch, "_safety_check"), \
                mock.patch.object(dispatch, "_target", return_value=(self.pane, "factory-primary")), \
                mock.patch.object(dispatch, "_send", side_effect=dispatch.StageError("pane unavailable")):
            with self.assertRaisesRegex(dispatch.StageError, "uncertain"):
                dispatch.handoff(self.cfg, self.c, "d1")
        self.assertEqual(scheduler.launch(self.c, "d1")["state"], "uncertain")
        with mock.patch.object(dispatch, "_safety_check"), \
                mock.patch.object(dispatch, "_target", return_value=(self.pane, "factory-primary")), \
                mock.patch.object(dispatch, "_send") as send, \
                self.assertRaisesRegex(dispatch.StageError, "uncertain send"):
            dispatch.handoff(self.cfg, self.c, "d1")
        send.assert_not_called()

    def test_release_unsent_recovers_an_uncertain_launch(self):
        self.c.execute("INSERT INTO dispatch_launch(run_id,pane_id,state,claimed_at) VALUES ('d1','w1:p1','uncertain',?)",
                       (SNAP,))
        panes = {"result": {"panes": [{"pane_id": "w1:p1", "agent": "omp", "agent_status": "idle"}]}}
        with mock.patch.object(dispatch, "_herdr", return_value=panes):
            res = dispatch.release_unsent(self.cfg, self.c, "d1", "user:cli", "never reached the pane")
        self.assertEqual(res["released"], "uncertain")
        self.assertIsNone(scheduler.launch(self.c, "d1"))

    def test_release_unsent_guards(self):
        self.c.execute("INSERT INTO dispatch_launch(run_id,pane_id,state,claimed_at) VALUES ('d1','w1:p1','sent',?)",
                       (SNAP,))
        with self.assertRaisesRegex(dispatch.StageError, "already sent"):
            dispatch.release_unsent(self.cfg, self.c, "d1", "user:cli", "reason")
        self.c.execute("UPDATE dispatch_launch SET state='uncertain' WHERE run_id='d1'")
        with self.assertRaisesRegex(dispatch.StageError, "human actor"):
            dispatch.release_unsent(self.cfg, self.c, "d1", "agent:factory", "reason")
        with self.assertRaisesRegex(dispatch.StageError, "say why"):
            dispatch.release_unsent(self.cfg, self.c, "d1", "user:cli", "  ")
        panes = {"result": {"panes": []}}  # the stored pane is unknown: refuse
        with mock.patch.object(dispatch, "_herdr", return_value=panes), \
                self.assertRaisesRegex(dispatch.StageError, "unknown"):
            dispatch.release_unsent(self.cfg, self.c, "d1", "user:cli", "reason")
        busy = {"result": {"panes": [{"pane_id": "w1:p1", "agent": "omp", "agent_status": "running"}]}}
        with mock.patch.object(dispatch, "_herdr", return_value=busy), \
                self.assertRaisesRegex(dispatch.StageError, "not releasing a live executor"):
            dispatch.release_unsent(self.cfg, self.c, "d1", "user:cli", "reason")


class ExecuteGuard(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.c = db.connect(Path(tmp.name) / "t.db")
        self.addCleanup(self.c.close)
        _v21.ensure_schema(self.c)

    def test_legacy_staged_execute_reserves_safely(self):
        body = b"# Dispatch d1\n"
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at,route) "
                       "VALUES ('d1','draft','[]','x',?,'fx-api')", (SNAP,))
        scheduler.set_claims(self.c, "d1", {"repo:a", "route:fx-api"})  # draft: pinned
        self.c.execute("UPDATE dispatch SET state='staged', body_sha256=?, approved_by='u', last_actor='p' "
                       "WHERE run_id='d1'", (hashlib.sha256(body).hexdigest(),))
        dispatches = Path(tempfile.mkdtemp())
        (dispatches / "d1").mkdir(parents=True)
        (dispatches / "d1" / "dispatch.md").write_bytes(body)
        with mock.patch.object(dispatch, "_safety_check"), \
                mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w", "HERDR_PANE_ID": "w:p1"}), \
                mock.patch.object(dispatch, "_lead_pane_id", return_value="w:p1"):
            res = dispatch.execute(SimpleNamespace(raw={}, dispatches=dispatches), self.c, "d1", "lead")
        self.assertEqual(res["executor_pane"], "w:p1")
        self.assertEqual(self.c.execute("SELECT state FROM dispatch").fetchone()[0], "executing")

    def test_resume_refuses_another_dispatchs_pane(self):
        _v21.seed_dispatch(self.c, "d1", "executing")
        _v21.seed_dispatch(self.c, "d2", "staged")
        self.c.execute("INSERT INTO dispatch_launch(run_id,pane_id,state,claimed_at) VALUES ('d2','w1:p1','sent',?)",
                       (SNAP,))
        pane = {"pane_id": "w1:p1", "agent": "omp", "agent_status": "idle"}
        with mock.patch.object(dispatch, "_target", return_value=(pane, "factory-primary")), \
                self.assertRaisesRegex(dispatch.StageError, "reserved for dispatch d2"):
            dispatch.resume(SimpleNamespace(raw={}), self.c, "d1")

    def test_resume_refuses_busy_fallback_pane(self):
        _v21.seed_dispatch(self.c, "d1", "executing")  # executor_pane is 'pane1', not the fallback
        pane = {"pane_id": "w1:p1", "agent": "omp", "agent_status": "running"}
        with mock.patch.object(dispatch, "_target", return_value=(pane, "factory-primary")), \
                self.assertRaisesRegex(dispatch.StageError, "unrelated work"):
            dispatch.resume(SimpleNamespace(raw={}), self.c, "d1")


class PruneBacklogBrief(unittest.TestCase):
    def test_approved_brief_source_without_association_enters_prune(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        c = db.connect(Path(tmp.name) / "t.db")
        self.addCleanup(c.close)
        _v21.ensure_schema(c)
        c.execute("INSERT INTO linear_project VALUES ('p','s','API','lead@x',?)", (SNAP,))
        c.execute("INSERT INTO repo_trunk VALUES ('o/api','main','deadbeef',?)", (SNAP,))
        _v21.seed_snapshot(c, "i1", "FIN-1", state_type="backlog", in_scope=0)  # Backlog source
        _v21.seed_brief(c, 1, ["FIN-1"])
        cfg = _v21.mkcfg(Path(tmp.name))
        with mock.patch.object(prune, "_brief_verify_targets", return_value=[
                {"brief_id": 1, "identifier": "FIN-1", "issue_id": "i1", "narrative": "# Brief 1"}]):
            got = prune.gate(cfg, c)
        items = {t["identifier"]: t for t in got["context"]["tickets"]}
        self.assertIn("FIN-1", items)
        self.assertEqual(items["FIN-1"]["brief_id"], 1)
        self.assertEqual(items["FIN-1"]["brief"], "# Brief 1")


if __name__ == "__main__":
    unittest.main()
