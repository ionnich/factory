"""Review step: nothing leaves draft unreviewed, a person's draft is never started without them, and an automatic
draft starts only once its review has been in front of the user for the window, unless they held it."""
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import db, decide, dispatch

SNAP = "2026-09-01T00:00:00Z"


class Review(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        self.c.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1',?,?,'unstarted',1,?)",
                       (SNAP, SNAP, '{"title": "t"}'))
        self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,repo,kind,reason,evidence_json,created_at,"
                       "created_by) VALUES ('i1',?,'r1','valid','r','[1]',?,'t')", (SNAP, SNAP))
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at,drafted_by,planned_at) "
                       "VALUES ('d1','draft','[]','x',?,?,?)", (SNAP, dispatch.PROPOSE, SNAP))
        self.c.execute("INSERT INTO dispatch_ticket(run_id,issue_id,identifier,snapshot_updated_at,verdict_id) "
                       "VALUES ('d1','i1','FIN-1',?,1)", (SNAP,))
        self.review = decide.review(self.c, "d1", "approve", "plan written", "t")
        self.cfg = SimpleNamespace(repos={}, raw={})

    def sweep(self):
        with mock.patch.object(dispatch, "approve", return_value={}) as ap, \
                mock.patch.object(dispatch, "start", return_value={}):
            res = decide.sweep(self.cfg, self.c)
        return res, ap

    def tell(self, hours_ago: float) -> list:
        """The digest reaches the user `hours_ago` (its clock starts then)."""
        return decide.notify(self.cfg, self.c, now=datetime.now(UTC) - timedelta(hours=hours_ago))

    def test_draft_leaves_review_only_approved_or_rejected(self):
        for sql in ("UPDATE dispatch SET state='staged', body_sha256='h' WHERE run_id='d1'",
                    "UPDATE dispatch SET state='archived', body_sha256='h' WHERE run_id='d1'"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.c.execute(sql)
        self.c.execute("UPDATE dispatch SET state='staged', body_sha256='h', approved_by='u' WHERE run_id='d1'")
        with self.assertRaises(sqlite3.IntegrityError):  # notes and plan close with the review
            self.c.execute("INSERT INTO dispatch_note(run_id,node_id,author,body,at) VALUES ('d1','root','u','n',?)",
                           (SNAP,))

    def test_person_draft_is_never_started_without_them(self):
        self.c.execute("UPDATE dispatch SET drafted_by='user' WHERE run_id='d1'")
        self.tell(30)
        res, ap = self.sweep()
        self.assertEqual(res, [])
        ap.assert_not_called()
        self.assertEqual(decide.one(self.c, self.review)["on_timeout"], "it waits for you")

    def test_auto_draft_starts_once_the_window_passed_since_the_digest(self):
        self.assertEqual(self.sweep()[0], [])  # not in front of the user yet: no clock
        self.assertIn(f"#{self.review} Start FIN-1? ★ Approve & start", self.tell(3)[0])
        with self.assertRaises(sqlite3.IntegrityError):  # the clock is set once
            self.c.execute("UPDATE decision SET due_at=? WHERE id=?", (SNAP, self.review))
        res, ap = self.sweep()
        self.assertEqual([(r["id"], r["chosen"]) for r in res], [(self.review, "approve")])
        ap.assert_called_once()
        self.assertTrue(decide.one(self.c, self.review)["chosen_by"].startswith(decide.AUTO))

    def test_window_still_open(self):
        self.tell(1)  # 1h of 2h
        self.assertEqual(self.sweep()[0], [])

    def test_holding_stops_the_clock(self):
        self.tell(3)
        decide.choose(self.cfg, self.c, self.review, "hold", "user", note="reading it")
        res, ap = self.sweep()
        self.assertEqual(res, [])
        ap.assert_not_called()
        held = decide.open_review(self.c, "d1")  # held: approve or reject, and no clock any more
        self.assertEqual([o["id"] for o in held["options"]], ["approve", "reject"])
        self.assertIsNone(held["deadline"])

    def test_window_end_takes_the_recommendation_even_when_it_is_not_approve(self):
        decide.void(self.c, "id=?", (self.review,), "replaced for the test")
        decide.review(self.c, "d1", "hold", "the plan guesses the schema", "agent:factory-plan")
        self.tell(3)
        res, ap = self.sweep()
        self.assertEqual(res[0]["chosen"], "hold")
        ap.assert_not_called()
        self.assertIn("the plan guesses the schema", self.c.execute("SELECT held_reason FROM dispatch").fetchone()[0])

    def test_plan_is_a_dag_under_the_dispatch_tickets(self):
        self.c.execute("UPDATE dispatch SET planned_at=NULL WHERE run_id='d1'")
        bad = ([{"id": "FIN-2/1", "title": "x"}],                                     # not this dispatch's ticket
               [{"id": "FIN-1/1.1", "title": "x"}],                                   # parent step missing
               [{"id": "FIN-1/1", "title": "a", "depends_on": ["FIN-1/2"]},
                {"id": "FIN-1/2", "title": "b", "depends_on": ["FIN-1/1"]}])          # cycle
        for steps in bad:
            with self.subTest(steps=steps), self.assertRaises(dispatch.StageError):
                dispatch.plan(self.c, "d1", steps)
        dispatch.plan(self.c, "d1", [{"id": "FIN-1/1", "title": "a"}, {"id": "FIN-1/1.1", "title": "a1"},
                                     {"id": "FIN-1/2", "title": "b", "depends_on": ["FIN-1/1"]}])
        self.assertEqual([(n["id"], n["parent"]) for n in dispatch.tree(self.c, "d1")],
                         [("root", None), ("FIN-1", "root"), ("FIN-1/1", "FIN-1"), ("FIN-1/1.1", "FIN-1/1"),
                          ("FIN-1/2", "FIN-1")])

    def test_plan_nests_tickets_and_drops_misfits(self):
        self.c.execute("UPDATE dispatch SET planned_at=NULL, repos_json=? WHERE run_id='d1'",
                       ('[{"repo": "r1", "trunk_sha": "a"}, {"repo": "r2", "trunk_sha": "b"}]',))
        for i, repo in ((2, "r1"), (3, "r2")):
            self.c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,'unstarted',1,?)",
                           (f"i{i}", f"FIN-{i}", SNAP, SNAP, '{"title": "t"}'))
            self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,repo,kind,reason,evidence_json,created_at,"
                           "created_by) VALUES (?,?,?,'valid','r','[1]',?,'t')", (f"i{i}", SNAP, repo, SNAP))
            self.c.execute("INSERT INTO dispatch_ticket(run_id,issue_id,identifier,snapshot_updated_at,verdict_id) "
                           "VALUES ('d1',?,?,?,?)", (f"i{i}", f"FIN-{i}", SNAP, i))
        with self.assertRaises(dispatch.StageError):  # tickets nested in a loop
            dispatch.plan(self.c, "d1", [{"id": "FIN-1", "under": "FIN-2"}, {"id": "FIN-2", "under": "FIN-1"},
                                         {"id": "FIN-1/1", "title": "a"}, {"id": "FIN-2/1", "title": "b"},
                                         {"id": "FIN-3/1", "title": "c"}])
        res = dispatch.plan(self.c, "d1", [
            {"id": "root", "title": "CORS hardening", "detail": "same surface"},
            {"id": "FIN-2", "under": "FIN-1", "depends_on": ["FIN-1"]}, {"id": "FIN-3", "exclude": "other domain"},
            {"id": "FIN-1/1", "title": "a"}, {"id": "FIN-2/1", "title": "b"}])
        self.assertEqual(res["tickets"], ["FIN-1", "FIN-2"])
        t = dispatch.tree(self.c, "d1")
        self.assertEqual([(n["id"], n["parent"]) for n in t],
                         [("root", None), ("FIN-1", "root"), ("FIN-1/1", "FIN-1"), ("FIN-2", "FIN-1"),
                          ("FIN-2/1", "FIN-2")])
        self.assertEqual(t[0]["title"], "CORS hardening")
        self.assertIn("FIN-3", t[0]["notes"][0]["body"])  # the drop is recorded for the reviewer
        self.assertEqual(self.c.execute("SELECT repos_json FROM dispatch WHERE run_id='d1'").fetchone()[0],
                         '[{"repo": "r1", "trunk_sha": "a"}]')


if __name__ == "__main__":
    unittest.main()
