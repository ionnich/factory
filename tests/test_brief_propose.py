"""Automatic grooming boundaries against real SQLite state; only the external model is replaced."""
import concurrent.futures
import contextlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import brief_propose, cli, db, strategy
from factory.config import Context
from factory.dispatch import StageError
from _helpers import (SNAP, body, mkcfg, seed_dispatch, seed_project, seed_snapshot, seed_ticket,
                      seed_trunk, seed_verdict)

NEXT = "2026-09-02T00:00:00Z"


class AutoBriefs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = mkcfg(Path(self.tmp.name))
        self.conn = db.connect(self.cfg.db)
        self.addCleanup(self.conn.close)
        seed_project(self.conn)
        seed_trunk(self.conn)

    def source(self, number, **kw):
        ident = f"FIN-{number}"
        seed_snapshot(self.conn, ident.lower(), ident, **kw)
        self.relationships(ident)
        return ident

    def relationships(self, ident, edges=()):
        from factory.relationships import _fingerprint
        self.conn.execute("INSERT OR REPLACE INTO linear_relationship VALUES (?,?,?,?,?)",
                          (ident, SNAP, json.dumps(edges), "[]", _fingerprint(list(edges))))

    def draft(self, identifiers):
        return strategy.create(self.cfg, self.conn, identifiers, "user:cli", body())

    def tick(self, effect=None):
        with mock.patch.object(strategy, "_run_groom", side_effect=effect, return_value=body()):
            return brief_propose.propose(self.cfg, self.conn)

    def test_raw_backlog_creates_one_review_draft_without_verification_or_execution(self):
        self.source(1, state_type="backlog", in_scope=0)
        first = self.tick()
        self.assertEqual(first["status"], "created")
        brief = strategy.get(self.conn, first["brief"]["id"])
        self.assertEqual((brief["state"], brief["created_by"], brief["approved_at"]),
                         ("draft", "agent:brief-proposer", None))
        self.assertIn("global:*", brief["body"]["resources"])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM brief_verdict").fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM dispatch").fetchone())
        self.assertEqual(self.tick()["status"], "idle")
        self.assertEqual([r["id"] for r in strategy.brief_reviews(self.conn)], [brief["id"]])

    def test_concurrent_ticks_do_not_duplicate_or_hold_sqlite_during_model(self):
        self.source(1)
        entered, release = threading.Event(), threading.Event()

        def model(sources):
            entered.set()
            if not release.wait(5):
                raise AssertionError("concurrent caller did not finish")
            return body()

        def worker():
            connection = db.connect(self.cfg.db)
            try:
                return brief_propose.propose(self.cfg, connection)
            finally:
                connection.close()

        with mock.patch.object(strategy, "_run_groom", side_effect=model):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(worker)
                try:
                    self.assertTrue(entered.wait(5))
                    # Another writer can commit while grooming is running.
                    with db.tx(self.conn):
                        self.conn.execute("UPDATE repo_trunk SET fetched_at=?", (NEXT,))
                    self.assertEqual(brief_propose.propose(self.cfg, self.conn)["status"], "busy")
                finally:
                    release.set()
                self.assertEqual(future.result(timeout=5)["status"], "created")
        self.assertEqual(self.tick()["status"], "idle")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_brief").fetchone()[0], 1)

    def test_cap_counts_only_latest_undismissed_manual_and_automatic_drafts(self):
        for i in range(1, 6):
            self.source(i)
        old = self.draft(["FIN-1"])
        latest = strategy.revise(self.cfg, self.conn, old["id"], body("revised"), None, "user:cli")
        self.draft(["FIN-2"])
        self.assertEqual(self.tick()["brief"]["sources"], ["FIN-3"])
        self.assertEqual(self.tick()["status"], "capped")
        strategy.dismiss(self.cfg, self.conn, latest["id"], "not needed", "user:cli")
        self.assertEqual(self.tick()["brief"]["sources"], ["FIN-4"])
        self.assertEqual(len(strategy.brief_reviews(self.conn)), 3)

    def test_ineligible_sources_cannot_starve_eligible_backlog(self):
        self.source(1, state_type="completed")
        self.source(2, state_type="canceled")
        self.source(3, state_name="Ready for QA")
        self.source(4, assignee={"email": "someone@else"})
        self.source(5, domain="Foreign")
        self.source(6)
        self.conn.execute("DELETE FROM linear_relationship WHERE identifier='FIN-6'")
        self.source(7)
        self.conn.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,kind,reason,evidence_json,created_at,"
                          "created_by) VALUES ('fin-7',?,'needs-clarification','unclear','[1]',?,'agent:prune')",
                          (SNAP, SNAP))
        self.source(8)
        verdict = seed_verdict(self.conn, "fin-8")
        seed_dispatch(self.conn, "live")
        seed_ticket(self.conn, "live", "fin-8", "FIN-8", verdict)
        self.source(9)
        self.assertEqual(self.tick()["brief"]["sources"], ["FIN-9"])

    def test_active_published_held_and_replacement_drafts_remain_covered(self):
        for i in range(1, 5):
            self.source(i)
        published = self.draft(["FIN-1"])
        strategy.approve(self.cfg, self.conn, published["id"], "user:cli")
        strategy.hold(self.cfg, self.conn, published["id"], "wait", "user:cli")
        strategy.revise(self.cfg, self.conn, published["id"], body(), "amend", "user:cli")
        self.draft(["FIN-2"])
        self.assertEqual(self.tick()["brief"]["sources"], ["FIN-3"])

    def test_dismissed_versions_stay_suppressed_until_source_changes(self):
        self.source(1)
        first = self.tick()["brief"]
        strategy.dismiss(self.cfg, self.conn, first["id"], "not now", "user:dashboard")
        self.assertEqual(self.tick()["status"], "idle")
        seed_snapshot(self.conn, "fin-1", "FIN-1", snap=NEXT)
        second = self.tick()["brief"]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(strategy.get(self.conn, second["id"])["sources"][0]["snapshot_updated_at"], NEXT)

    def test_archived_legacy_work_and_own_writes_are_not_new_work(self):
        self.source(1)
        verdict = seed_verdict(self.conn, "fin-1")
        seed_dispatch(self.conn, "old")
        seed_ticket(self.conn, "old", "fin-1", "FIN-1", verdict)
        self.conn.execute("UPDATE dispatch SET state='archived',body_sha256='sha',rejected_reason='retired' "
                          "WHERE run_id='old'")
        self.assertEqual(self.tick()["status"], "idle")
        seed_snapshot(self.conn, "fin-1", "FIN-1", snap=NEXT)
        self.conn.execute("INSERT INTO linear_own_write VALUES ('fin-1',?)", (NEXT,))
        self.assertEqual(self.tick()["status"], "idle")
        seed_snapshot(self.conn, "fin-1", "FIN-1", snap="2026-09-03T00:00:00Z")
        self.assertEqual(self.tick()["status"], "created")

    def test_project_context_buckets_are_singletons_and_recorded_links_can_group(self):
        self.source(1)
        self.source(2)
        self.assertEqual(self.tick()["brief"]["sources"], ["FIN-1"])
        self.source(3)
        edges = [{"kind": "blocks", "source": "FIN-2", "target": "FIN-3"}]
        self.relationships("FIN-2", edges)
        self.relationships("FIN-3", edges)
        self.assertEqual(self.tick()["brief"]["sources"], ["FIN-2", "FIN-3"])

    def test_relationship_groups_split_context_repo_and_route_boundaries(self):
        self.cfg.contexts.append(Context("other", "o/other", domains=["Other"], route="fx-other"))
        self.conn.execute("INSERT INTO linear_project VALUES ('q','other','Other','lead@x',?)", (SNAP,))
        self.source(1)
        self.source(2, domain="Other")
        edges = [{"kind": "related", "source": "FIN-1", "target": "FIN-2"}]
        for ident in ("FIN-1", "FIN-2"):
            self.relationships(ident, edges)
        self.assertEqual(self.tick()["brief"]["sources"], ["FIN-1"])
        self.assertEqual(self.tick()["brief"]["sources"], ["FIN-2"])

    def test_todo_precedes_urgent_backlog_then_backlog_remains_eligible(self):
        self.source(2)
        self.source(3)
        raw = json.loads(self.conn.execute("SELECT raw_json FROM linear_snapshot WHERE identifier='FIN-2'").fetchone()[0])
        raw.update(identifier="FIN-1", title="Urgent backlog", priority=1, state={"name": "Backlog", "type": "backlog"})
        self.conn.execute("INSERT INTO linear_snapshot VALUES ('fin-1','FIN-1',?,?,'backlog',0,?)",
                          (SNAP, SNAP, json.dumps(raw)))
        self.relationships("FIN-1")
        self.conn.execute("INSERT INTO linear_due VALUES ('fin-1',?,'2026-08-01')", (SNAP,))
        self.conn.execute("INSERT INTO linear_due VALUES ('fin-3',?,'2026-09-01')", (SNAP,))
        self.assertEqual(self.tick()["brief"]["sources"], ["FIN-3"])
        self.assertEqual(self.tick()["brief"]["sources"], ["FIN-2"])
        self.assertEqual(self.tick()["brief"]["sources"], ["FIN-1"])

    def test_large_related_group_is_bounded_and_due_date_breaks_priority_ties(self):
        for number in range(1, 14):
            self.source(number)
        edges = [{"kind": "parent", "source": "FIN-1", "target": f"FIN-{number}"}
                 for number in range(2, 14)]
        for number in range(1, 14):
            self.relationships(f"FIN-{number}", edges)
        self.conn.execute("INSERT INTO linear_due VALUES ('fin-2',?,'2026-09-01')", (SNAP,))
        first = self.tick()["brief"]["sources"]
        self.assertEqual(len(first), 12)
        self.assertEqual(first[0], "FIN-2")
        second = self.tick()["brief"]["sources"]
        self.assertEqual(set(first) | set(second), {f"FIN-{number}" for number in range(1, 14)})
        self.assertFalse(set(first) & set(second))

    def test_cycles_and_oversized_sources_do_not_starve_other_candidates(self):
        self.source(1)
        self.source(2)
        self.relationships("FIN-1", [{"kind": "blocks", "source": "FIN-1", "target": "FIN-1"}])
        seed_snapshot(self.conn, "fin-2", "FIN-2", snap=NEXT, domain="API\n" + "x" * 260000)
        self.source(3)
        result = self.tick()
        self.assertEqual(result["brief"]["sources"], ["FIN-3"])
        self.assertEqual({tuple(s["sources"]) for s in result["skipped"]}, {("FIN-1",), ("FIN-2",)})

    def test_model_cannot_invent_membership_dependencies_or_resource_claims(self):
        self.source(1)
        with self.assertRaises(StageError):
            self.tick(lambda _: body(dependencies=["FIN-999"]))
        self.assertEqual(strategy.brief_reviews(self.conn), [])
        result = self.tick(lambda _: body(resources=["repo:untrusted"]))
        brief = strategy.get(self.conn, result["brief"]["id"])
        self.assertEqual(brief["body"]["resources"], ["repo:o/api", "route:fx-api", "global:*"])
        self.assertEqual([s["identifier"] for s in brief["sources"]], ["FIN-1"])

    def test_model_time_changes_discard_stale_result(self):
        for change in ("snapshot", "relationship", "ownership", "coverage", "cap", "verdict"):
            with self.subTest(change=change):
                # Each race gets its own real database and source history.
                with tempfile.TemporaryDirectory() as tmp:
                    cfg = mkcfg(Path(tmp))
                    connection = db.connect(cfg.db)
                    try:
                        seed_project(connection)
                        seed_snapshot(connection, "fin-1", "FIN-1")
                        connection.execute("INSERT INTO linear_relationship VALUES ('FIN-1',?,'[]','[]','fp')", (SNAP,))

                        def model(sources):
                            if change == "snapshot":
                                seed_snapshot(connection, "fin-1", "FIN-1", snap=NEXT)
                            elif change == "relationship":
                                connection.execute("UPDATE linear_relationship SET fingerprint='changed'")
                            elif change == "ownership":
                                connection.execute("UPDATE linear_project SET lead_email='other@person'")
                            elif change in ("coverage", "cap"):
                                for _ in range(3 if change == "cap" else 1):
                                    strategy.create(cfg, connection, ["FIN-1"], "user:cli", body("Human"))
                            elif change == "verdict":
                                connection.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,kind,reason,"
                                                   "evidence_json,created_at,created_by) "
                                                   "VALUES ('fin-1',?,'already-done','done','[1]',?,'agent:prune')",
                                                   (SNAP, SNAP))
                            return body()

                        with mock.patch.object(strategy, "_run_groom", side_effect=model):
                            result = brief_propose.propose(cfg, connection)
                        self.assertEqual(result["status"], "capped" if change == "cap" else "stale")
                        self.assertIsNone(connection.execute("SELECT 1 FROM work_brief WHERE created_by=?",
                                                             (brief_propose.ACTOR,)).fetchone())
                    finally:
                        connection.close()

    def test_human_dismissal_preserves_history_and_refuses_approval_revision_and_sql_bypass(self):
        self.source(1)
        original = self.draft(["FIN-1"])
        for actor in ("agent:brief-proposer", "factory:propose"):
            with self.assertRaises(StageError):
                strategy.dismiss(self.cfg, self.conn, original["id"], "no", actor)
        with self.assertRaises(StageError):
            strategy.dismiss(self.cfg, self.conn, original["id"], "  ", "user:cli")
        result = strategy.dismiss(self.cfg, self.conn, original["id"], "Not useful", "user:dashboard")
        self.assertEqual((result["body"], result["sources"], result["state"]),
                         (original["body"], original["sources"], "draft"))
        self.assertEqual(result["dismissal"]["reason"], "Not useful")
        self.assertEqual(strategy.brief_reviews(self.conn), [])
        summary = strategy.overview(self.cfg, self.conn)["briefs"][0]
        self.assertEqual((summary["readiness"], summary["dismissal"]), ("dismissed", result["dismissal"]))
        with self.assertRaises(StageError):
            strategy.approve(self.cfg, self.conn, result["id"], "user:dashboard")
        with self.assertRaises(StageError):
            strategy.revise(self.cfg, self.conn, result["id"], body(), "revise", "user:cli")
        for sql in ("UPDATE work_brief SET state='approved',approved_by='user',approved_at='now' WHERE id=?",
                    "UPDATE brief_dismissal SET reason='changed' WHERE brief_id=?",
                    "DELETE FROM brief_dismissal WHERE brief_id=?"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql, (result["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO work_brief(revision,parent_id,state,body_json,sources_json,created_at,"
                              "created_by) SELECT 2,id,'draft',body_json,sources_json,created_at,created_by "
                              "FROM work_brief WHERE id=?", (result["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT OR REPLACE INTO brief_dismissal VALUES (?,?,?,?)",
                              (result["id"], "overwritten", "user:cli", SNAP))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT OR REPLACE INTO work_brief(id,revision,parent_id,state,body_json,sources_json,"
                              "created_at,created_by,approved_at,approved_by) "
                              "SELECT id,revision,parent_id,'approved',body_json,sources_json,created_at,created_by,"
                              "'now','user:cli' FROM work_brief WHERE id=?", (result["id"],))

    def test_only_latest_draft_can_be_dismissed(self):
        self.source(1)
        old = self.draft(["FIN-1"])
        latest = strategy.revise(self.cfg, self.conn, old["id"], body(), None, "user:cli")
        with self.assertRaises(StageError):
            strategy.dismiss(self.cfg, self.conn, old["id"], "old", "user:cli")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO brief_dismissal VALUES (?,?,?,?)", (old["id"], "old", "user:cli", SNAP))
        strategy.approve(self.cfg, self.conn, latest["id"], "user:cli")
        with self.assertRaises(StageError):
            strategy.dismiss(self.cfg, self.conn, latest["id"], "published", "user:cli")

    def test_propose_announces_creation_once_and_model_failures_are_visible(self):
        self.source(1)
        self.cfg.raw["notify"] = {"url": "https://dashboard.example/factory?workspace=work"}
        with contextlib.ExitStack() as stack:
            for name in ("ingest", "decide.acknowledge_notifications", "decide.sweep", "jev.refresh",
                         "costs.sync", "learn.sync"):
                stack.enter_context(mock.patch("factory.cli." + name, return_value=[]))
            stack.enter_context(mock.patch("factory.cli.dispatch.propose", return_value={}))
            stack.enter_context(mock.patch("factory.cli.decide.notify", side_effect=lambda *a, **kw: ["Existing notice"]))
            stack.enter_context(mock.patch("factory.cli.decide.notification_execution", return_value=None))
            stack.enter_context(mock.patch.object(strategy, "_run_groom", return_value=body()))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cli.cmd_propose(self.cfg, self.conn, SimpleNamespace(announce=False))
            result = json.loads(output.getvalue())
            self.assertEqual(result["brief_proposal"]["status"], "created")
            self.assertIn("https://dashboard.example/factory?workspace=work&stage=strategy&brief=1", result["messages"][1])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cli.cmd_propose(self.cfg, self.conn, SimpleNamespace(announce=True))
            self.assertNotIn("https://dashboard.example/factory", output.getvalue())
            self.source(2)
            with mock.patch.object(strategy, "_run_groom", side_effect=StageError("model unavailable")):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                    cli.cmd_propose(self.cfg, self.conn, SimpleNamespace(announce=False))
                failed = json.loads(output.getvalue())
                self.assertEqual(failed["brief_proposal"]["status"], "failed")
                self.assertIn("model unavailable", failed["messages"][-1])

    def test_cli_dismiss_and_approve_enforce_real_durable_boundary(self):
        self.source(1)
        brief = self.draft(["FIN-1"])
        with mock.patch.object(cli.config, "load", return_value=self.cfg), \
                mock.patch.object(cli.db, "connect", return_value=self.conn):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exited:
                cli.main(["strategy", "dismiss", str(brief["id"]), "--reason", "Not useful",
                          "--actor", "user:dashboard"])
            self.assertEqual(exited.exception.code, 0)
            self.assertEqual(json.loads(output.getvalue())["dismissal"]["actor"], "user:dashboard")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as refused:
                cli.main(["strategy", "approve", str(brief["id"]), "--actor", "user:dashboard"])
            self.assertEqual(refused.exception.code, 1)
            self.assertEqual(strategy.get(self.conn, brief["id"])["state"], "draft")


if __name__ == "__main__":
    unittest.main()
