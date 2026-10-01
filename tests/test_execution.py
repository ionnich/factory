"""Execution slice behavioral regressions: hierarchical resource conflict, bounded capacity admission with a single
winner, exact-version brief verification (real strategy.create/approve + brief_verdict), the bounded propose queue
with blocked-first independence, uncertain-send safety, and the direct execute/resume pane guards."""
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
import _helpers  # noqa: E402

from factory import db, dispatch, prune, scheduler

SNAP = _helpers.SNAP


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

    def staged(self, run_id, resources, route=None):
        _helpers.seed_dispatch(self.c, run_id, "draft", route=route)
        _helpers.set_claims(self.c, run_id, resources)
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
        self.c.execute("UPDATE dispatch SET state='executing', executor_pane='p1', executing_at=? WHERE run_id='d1'",
                       (SNAP,))
        self.assertEqual(scheduler.capacity_used(self.c), 2)  # executing + own launch counts once

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

    def test_terminal_frees_capacity_claims_held_until_archive(self):
        _helpers.seed_snapshot(self.c, "i1", "FIN-1")
        v = _helpers.seed_verdict(self.c, "i1")
        _helpers.seed_dispatch(self.c, "d1", "draft")
        _helpers.set_claims(self.c, "d1", {"repo:a", "route:fx-a"})
        _helpers.seed_ticket(self.c, "d1", "i1", "FIN-1", v)
        self.c.execute("UPDATE dispatch SET state='staged', body_sha256='h', approved_by='u', last_actor='p' "
                       "WHERE run_id='d1'")
        self.assertIsNone(scheduler.reserve_if_free(self.c, "d1", "p1", os.getpid()))
        self.c.execute("UPDATE dispatch SET state='executing', executor_pane='p1', executing_at=? WHERE run_id='d1'",
                       (SNAP,))
        self.assertEqual(scheduler.capacity_used(self.c), 1)
        self.c.execute("UPDATE dispatch_ticket SET card_status='blocked' WHERE run_id='d1'")  # auto-done -> done
        self.assertEqual(self.c.execute("SELECT state FROM dispatch WHERE run_id='d1'").fetchone()[0], "done")
        self.assertEqual(scheduler.capacity_used(self.c), 0)  # terminal frees capacity
        self.staged("d2", {"repo:a", "route:fx-b"})  # claims still conflict until archive
        self.assertIn("resource conflict", scheduler.reserve_if_free(self.c, "d2", "p2", os.getpid()))

    def test_concurrent_reserve_single_winner(self):
        self.c.execute("UPDATE execution_policy SET max_parallel=1")
        self.staged("d1", {"repo:a", "route:fx-a"})
        self.staged("d2", {"repo:b", "route:fx-b"})
        barrier = threading.Barrier(2)
        results, errors = [], []

        def worker(run, pane):
            barrier.wait()  # both threads ready before either opens a connection
            conn = None
            try:
                conn = db.connect(self.path)  # each worker owns its connection (sqlite forbids cross-thread sharing)
                results.append((run, scheduler.reserve_if_free(conn, run, pane, os.getpid())))
            except Exception as e:  # propagate thread failures, never swallow
                errors.append(e)
            finally:
                if conn is not None:
                    conn.close()

        threads = [threading.Thread(target=worker, args=(run, pane)) for run, pane in (("d1", "p1"), ("d2", "p2"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len([r for r in results if r[1] is None]), 1)  # exactly one winner
        self.assertEqual(len([r for r in results if r[1] is not None]), 1)  # the other rejected by capacity
        self.assertEqual(self.c.execute("SELECT count(*) FROM dispatch_launch").fetchone()[0], 1)


class StageBrief(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp = Path(tmp.name)
        self.c = db.connect(tmp / "t.db")
        self.addCleanup(self.c.close)
        _helpers.seed_project(self.c)
        _helpers.seed_trunk(self.c)
        _helpers.seed_snapshot(self.c, "i1", "FIN-1")
        self.verdict = _helpers.seed_verdict(self.c, "i1")
        self.cfg = _helpers.mkcfg(tmp)

    def test_stage_brief_requires_exact_association(self):
        brief = _helpers.publish_brief(self.cfg, self.c, ["FIN-1"])  # real, approved, but unverified
        with self.assertRaisesRegex(dispatch.StageError, "no recorded verification"):
            dispatch.stage_brief(self.cfg, self.c, brief["id"], "user")

    def test_stage_brief_pins_association_and_resources(self):
        brief = _helpers.publish_brief(self.cfg, self.c, ["FIN-1"])
        _helpers.associate(self.c, brief["id"], "i1", self.verdict)
        res = dispatch.stage_brief(self.cfg, self.c, brief["id"], "user")
        self.assertEqual(res["brief_id"], brief["id"])
        row = self.c.execute("SELECT brief_id, state FROM dispatch WHERE run_id=?", (res["run_id"],)).fetchone()
        self.assertEqual((row["brief_id"], row["state"]), (brief["id"], "draft"))
        self.assertEqual(scheduler.claims(self.c, res["run_id"]), {"global:*", "repo:o/api", "route:fx-api"})

    def test_new_amendment_needs_new_verification(self):
        from factory import strategy
        brief1 = _helpers.publish_brief(self.cfg, self.c, ["FIN-1"])
        _helpers.associate(self.c, brief1["id"], "i1", self.verdict)  # v1 verified
        draft2 = strategy.revise(self.cfg, self.c, brief1["id"], _helpers.body(title="T2"), "amend", _helpers.HUMAN)
        brief2 = strategy.approve(self.cfg, self.c, draft2["id"], _helpers.HUMAN)  # approved amendment, unverified
        with self.assertRaisesRegex(dispatch.StageError, "no recorded verification"):
            dispatch.stage_brief(self.cfg, self.c, brief2["id"], "user")  # fresh source verdict is not borrowed
        with self.assertRaisesRegex(dispatch.StageError, "superseded by a newer revision"):
            dispatch.stage_brief(self.cfg, self.c, brief1["id"], "user")

    def test_stage_brief_refuses_source_changed_since_capture(self):
        brief = _helpers.publish_brief(self.cfg, self.c, ["FIN-1"])
        _helpers.associate(self.c, brief["id"], "i1", self.verdict)
        self.c.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1',?,?,?,?,?)",
                       ("2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z", "unstarted", 1,
                        json.dumps({"identifier": "FIN-1", "title": "x", "state": {"name": "Todo"},
                                    "team": {"key": "FIN"}, "assignee": None, "labels": {"nodes": []},
                                    "description": "Domain: API"})))
        with self.assertRaisesRegex(dispatch.StageError, "changed since the brief was captured"):
            dispatch.stage_brief(self.cfg, self.c, brief["id"], "user")

    def test_stage_explicit_brief_rejects_non_approved(self):
        from factory import strategy
        draft = strategy.create(self.cfg, self.c, ["FIN-1"], _helpers.HUMAN, body=_helpers.body())  # draft, not approved
        with self.assertRaisesRegex(dispatch.StageError, "not approved"):
            dispatch.stage(self.cfg, self.c, ["FIN-1"], "user", brief_id=draft["id"])  # no bypass via explicit id

    def test_stage_identifiers_resolve_exact_approved_brief(self):
        brief = _helpers.publish_brief(self.cfg, self.c, ["FIN-1"])
        _helpers.associate(self.c, brief["id"], "i1", self.verdict)
        res = dispatch.stage(self.cfg, self.c, ["FIN-1"], "user")
        self.assertEqual(res["brief_id"], brief["id"])

    def test_stage_without_matching_brief_refuses(self):
        with self.assertRaisesRegex(dispatch.StageError, "no approved brief matches exactly"):
            dispatch.stage(self.cfg, self.c, ["FIN-1"], "user")


class Propose(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.c = db.connect(Path(tmp.name) / "t.db")
        self.addCleanup(self.c.close)
        _helpers.seed_project(self.c)
        _helpers.seed_trunk(self.c)
        _helpers.seed_snapshot(self.c, "i1", "FIN-1")
        self.verdict = _helpers.seed_verdict(self.c, "i1")
        self.cfg = _helpers.mkcfg(Path(tmp.name))

    def test_handoff_does_not_starve_independent_second(self):
        _helpers.seed_dispatch(self.c, "d1", "staged")
        _helpers.seed_dispatch(self.c, "d2", "staged")

        def fake_handoff(cfg, conn, run_id):
            if run_id == "d1":
                raise dispatch.StageError("resource conflict: repo:a held by another dispatch")
            return {"run_id": run_id}

        with mock.patch.object(dispatch, "handoff", side_effect=fake_handoff), \
                mock.patch.object(dispatch, "release_safe_terminal", return_value=[]):
            res = dispatch.propose(self.cfg, self.c)
        self.assertEqual([(h["run_id"], "blocked" in h) for h in res["handoffs"]],
                         [("d1", True), ("d2", False)])

    def test_prepare_skips_blocked_brief_and_prepares_next(self):
        from factory import strategy
        held = _helpers.publish_brief(self.cfg, self.c, ["FIN-1"])
        strategy.hold(self.cfg, self.c, held["id"], "waiting", _helpers.HUMAN)  # not intent-ready
        ready = _helpers.publish_brief(self.cfg, self.c, ["FIN-1"])
        _helpers.associate(self.c, ready["id"], "i1", self.verdict)

        def fake_stage(cfg, conn, brief_id, actor):
            return {"run_id": f"r{brief_id}", "brief_id": brief_id}

        with mock.patch.object(dispatch, "stage_brief", side_effect=fake_stage), \
                mock.patch.object(dispatch, "handoff", return_value={}), \
                mock.patch.object(dispatch, "release_safe_terminal", return_value=[]):
            res = dispatch.propose(self.cfg, self.c)
        self.assertEqual(res["prepared"], [{"brief_id": ready["id"], "run_id": f"r{ready['id']}"}])
        self.assertEqual([b["brief_id"] for b in res["blocked"]], [held["id"]])

    def test_prepare_respects_queue_cap(self):
        _helpers.seed_dispatch(self.c, "d1", "draft")
        _helpers.seed_dispatch(self.c, "d2", "draft")
        _helpers.seed_dispatch(self.c, "d3", "staged")  # 3 nonexecuting already: nothing more prepared
        with mock.patch.object(dispatch, "stage_brief") as stage, \
                mock.patch.object(dispatch, "handoff", return_value={}), \
                mock.patch.object(dispatch, "release_safe_terminal", return_value=[]):
            dispatch.propose(self.cfg, self.c)
        stage.assert_not_called()


class UncertainSend(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "t.db"
        self.c = db.connect(self.path)
        self.addCleanup(self.c.close)
        _helpers.seed_dispatch(self.c, "d1", "draft")
        _helpers.set_claims(self.c, "d1", {"repo:a", "route:fx-a"})
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
        with mock.patch.object(dispatch, "_herdr", return_value={"result": {"panes": []}}), \
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

    def test_legacy_staged_execute_reserves_safely(self):
        body = b"# Dispatch d1\n"
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at,route) "
                       "VALUES ('d1','draft','[]','x',?,'fx-api')", (SNAP,))
        _helpers.set_claims(self.c, "d1", {"repo:a", "route:fx-api"})  # pinned while draft
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
        _helpers.seed_dispatch(self.c, "d1", "executing", resources={"repo:a", "route:fx-a"}, pane="pane-1")
        _helpers.seed_dispatch(self.c, "d2", "staged", resources={"repo:b", "route:fx-b"})
        self.c.execute("INSERT INTO dispatch_launch(run_id,pane_id,state,claimed_at) VALUES ('d2','w1:p1','sent',?)",
                       (SNAP,))
        pane = {"pane_id": "w1:p1", "agent": "omp", "agent_status": "idle"}
        with mock.patch.object(dispatch, "_target", return_value=(pane, "factory-primary")), \
                mock.patch.object(dispatch, "_send") as send, \
                mock.patch.object(dispatch, "_herdr") as herdr, \
                self.assertRaises(dispatch.StageError):
            dispatch.resume(SimpleNamespace(raw={}), self.c, "d1")
        send.assert_not_called()  # no reset, no steal of the other run's pane
        herdr.assert_not_called()
        self.assertEqual(self.c.execute("SELECT executor_pane FROM dispatch WHERE run_id='d1'").fetchone()[0],
                         "pane-1")

    def test_resume_refuses_busy_fallback_pane(self):
        _helpers.seed_dispatch(self.c, "d1", "executing", resources={"repo:a", "route:fx-a"}, pane="pane-1")
        pane = {"pane_id": "w1:p1", "agent": "omp", "agent_status": "running"}
        with mock.patch.object(dispatch, "_target", return_value=(pane, "factory-primary")), \
                mock.patch.object(dispatch, "_send") as send, \
                mock.patch.object(dispatch, "_herdr") as herdr, \
                self.assertRaises(dispatch.StageError):
            dispatch.resume(SimpleNamespace(raw={}), self.c, "d1")
        send.assert_not_called()  # a busy fallback pane is never reset or sent to
        herdr.assert_not_called()


class PruneBacklogBrief(unittest.TestCase):
    def test_approved_brief_source_without_association_enters_prune(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        c = db.connect(Path(tmp.name) / "t.db")
        self.addCleanup(c.close)
        _helpers.seed_project(c)
        _helpers.seed_trunk(c)
        _helpers.seed_snapshot(c, "i1", "FIN-1", state_type="backlog", in_scope=0)  # Backlog source
        cfg = _helpers.mkcfg(Path(tmp.name))
        _helpers.publish_brief(cfg, c, ["FIN-1"])  # real approved brief, unverified
        got = prune.gate(cfg, c)
        items = {t["identifier"]: t for t in got["context"]["tickets"]}
        self.assertIn("FIN-1", items)
        self.assertIn("brief_id", items["FIN-1"])


class IntentVersionLineage(unittest.TestCase):
    """An approved ancestor superseded by a later approved descendant THROUGH an intermediate draft revision must
    never be re-offered or accepted. Lineage: #1 APPROVED -> #2 DRAFT -> #3 APPROVED; authoritative head is #3."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp = Path(tmp.name)
        self.c = db.connect(tmp / "t.db")
        self.addCleanup(self.c.close)
        _helpers.seed_project(self.c)
        _helpers.seed_trunk(self.c)
        _helpers.seed_snapshot(self.c, "i1", "FIN-1")
        self.verdict = _helpers.seed_verdict(self.c, "i1")
        self.cfg = _helpers.mkcfg(tmp)
        from factory import strategy
        self.b1 = strategy.approve(self.cfg, self.c,
                                   strategy.create(self.cfg, self.c, ["FIN-1"], _helpers.HUMAN,
                                                   body=_helpers.body(title="v1"))["id"], _helpers.HUMAN)
        self.b2 = strategy.revise(self.cfg, self.c, self.b1["id"], _helpers.body(title="v2"),
                                  "amend to a draft", _helpers.HUMAN)  # still a draft: does not supersede
        self.b3 = strategy.approve(self.cfg, self.c,
                                   strategy.revise(self.cfg, self.c, self.b2["id"], _helpers.body(title="v3"),
                                                   "amend again", _helpers.HUMAN)["id"], _helpers.HUMAN)

    def test_identifier_resolution_selects_current_head_not_ambiguous(self):
        self.assertEqual([b["id"] for b in dispatch._approved_unconsumed(self.c)], [self.b3["id"]])
        self.assertEqual(dispatch._resolve_brief(self.c, ["FIN-1"]), self.b3["id"])

    def test_explicit_stage_of_superseded_ancestor_refuses(self):
        with self.assertRaises(dispatch.StageError):
            dispatch.stage_brief(self.cfg, self.c, self.b1["id"], "user")

    def test_prune_put_superseded_ancestor_refuses_without_mutation(self):
        before_verdicts = self.c.execute("SELECT count(*) FROM verdict").fetchone()[0]
        before_links = self.c.execute("SELECT count(*) FROM brief_verdict").fetchone()[0]
        with self.assertRaises(prune.VerdictError):
            prune.put(self.cfg, self.c, "FIN-1", "valid", "r", [], brief_id=self.b1["id"])
        self.assertEqual(self.c.execute("SELECT count(*) FROM verdict").fetchone()[0], before_verdicts)
        self.assertEqual(self.c.execute("SELECT count(*) FROM brief_verdict").fetchone()[0], before_links)

    def test_current_head_stages_once_its_own_version_is_verified(self):
        _helpers.associate(self.c, self.b3["id"], "i1", self.verdict)
        res = dispatch.stage_brief(self.cfg, self.c, self.b3["id"], "user")
        self.assertEqual(res["brief_id"], self.b3["id"])

    def test_writing_transaction_revalidates_supersession(self):
        # Bypass the read gate (as a concurrent amend could) and drive the inner writer directly: its transaction
        # re-validation must still refuse the superseded ancestor even though the ancestor is itself approved+verified.
        _helpers.associate(self.c, self.b1["id"], "i1", self.verdict)
        with self.assertRaises(dispatch.StageError):
            dispatch._create_brief_draft(self.cfg, self.c, ["FIN-1"], "user", False, self.b1["id"],
                                         dispatch._brief(self.c, self.b1["id"]))
        self.assertEqual(self.c.execute("SELECT count(*) FROM dispatch").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
