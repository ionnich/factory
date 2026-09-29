"""Decisions: always a real choice with a recommendation, answered once, and the review's answer carries the
planner's open questions with it."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import db, decide, dispatch

SNAP = "2026-09-01T00:00:00Z"
OPTS = [decide.option("a", "A", "leads to a"), decide.option("b", "B", "leads to b", note="why b?")]


class Decisions(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        self.c.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1',?,?,'unstarted',1,?)",
                       (SNAP, SNAP, '{"title": "t"}'))
        self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,repo,kind,reason,evidence_json,created_at,"
                       "created_by) VALUES ('i1',?,'r1','valid','r','[1]',?,'t')", (SNAP, SNAP))
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at,drafted_by) "
                       "VALUES ('d1','draft','[]','x',?,'user')", (SNAP,))
        self.c.execute("INSERT INTO dispatch_ticket(run_id,issue_id,identifier,snapshot_updated_at,verdict_id) "
                       "VALUES ('d1','i1','FIN-1',?,1)", (SNAP,))
        self.cfg = SimpleNamespace(repos={}, raw={})

    def insert(self, options, recommended="a"):
        return decide.open_(self.c, "ask", "q?", options, recommended, "because", "t", run_id="d1")

    def test_a_decision_is_a_real_choice_with_a_recommendation(self):
        bad = [(OPTS[:1], "a"),                                                     # one option is no choice
               (OPTS, "c"),                                                         # recommends something else
               ([OPTS[0], {"id": "b", "label": "B"}], "a"),                          # an option without its outcome
               ([OPTS[0], {**OPTS[1], "id": "a"}], "a")]                             # same option twice
        for options, rec in bad:
            with self.subTest(options=options, rec=rec), self.assertRaises(sqlite3.IntegrityError):
                self.insert(options, rec)

    def test_answered_once_with_one_of_its_options_and_the_text_it_asks_for(self):
        did = self.insert(OPTS)
        for sql in ("UPDATE decision SET chosen='c', chosen_by='u', chosen_at='t' WHERE id=?",   # not an option
                    "UPDATE decision SET chosen='b', chosen_by='u', chosen_at='t' WHERE id=?",   # b needs a note
                    "UPDATE decision SET question='other?' WHERE id=?",
                    "DELETE FROM decision WHERE id=?"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.c.execute(sql, (did,))
        self.c.execute("UPDATE decision SET chosen='b', chosen_by='u', chosen_at='t', chosen_note='n' WHERE id=?",
                       (did,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("UPDATE decision SET chosen='a', chosen_note=NULL WHERE id=?", (did,))

    def plan(self, recommend="approve"):
        return dispatch.plan(self.c, "d1", [
            {"id": "root", "title": "theme", "recommend": recommend, "why": "small and safe"},
            {"id": "FIN-1/1", "title": "a"}, {"id": "FIN-1/2", "title": "b", "depends_on": ["FIN-1/1"]},
            {"question": "Real engine or mock?", "on": "FIN-1/2", "recommend": "real",
             "why": "the bug only shows on the engine", "options": [
                 {"id": "real", "label": "Real engine", "leads_to": "slower CI, catches the bug"},
                 {"id": "mock", "label": "Mock", "leads_to": "fast, misses the bug"}]}])

    def test_planner_questions_need_options_and_a_recommendation(self):
        for q in ({"question": "x?", "options": OPTS[:1], "recommend": "a", "why": "w"},
                  {"question": "x?", "options": [dict(o) for o in OPTS], "recommend": "a", "why": "w"},  # note key
                  {"question": "x?", "on": "FIN-9/1", "options": [OPTS[0], {**OPTS[0], "id": "z"}],
                   "recommend": "a", "why": "w"}):
            with self.subTest(q=q), self.assertRaises(dispatch.StageError):
                dispatch.plan(self.c, "d1", [{"id": "FIN-1/1", "title": "a"}, q])

    def test_approving_takes_the_open_questions_recommendations_with_it(self):
        self.plan()
        review, question = sorted(decide.rows(self.c, "d1"), key=lambda d: d["kind"] != "review")
        self.assertEqual((review["recommended"], review["why"]), ("approve", "small and safe"))
        self.assertEqual(question["node_id"], "FIN-1/2")
        with mock.patch.object(dispatch, "approve", return_value={}), \
                mock.patch.object(dispatch, "start", return_value={"handoff": "sent"}):
            res = decide.choose(self.cfg, self.c, review["id"], "approve", "user")
        self.assertEqual(res["handoff"], "sent")
        q = decide.one(self.c, question["id"])
        self.assertEqual(q["chosen"], "real")
        self.assertIn("recommendation", q["chosen_by"])

    def test_a_failed_effect_leaves_the_decision_open(self):
        self.plan()
        review = decide.open_review(self.c, "d1")
        with mock.patch.object(dispatch, "approve", side_effect=dispatch.StageError("FIN-1 changed")):
            with self.assertRaises(dispatch.StageError):
                decide.choose(self.cfg, self.c, review["id"], "approve", "user")
        self.assertTrue(decide.one(self.c, review["id"])["open"])
        self.assertTrue(all(d["open"] for d in decide.rows(self.c, "d1")))

    def test_rejecting_withdraws_the_drafts_other_questions(self):
        self.plan()
        review = decide.open_review(self.c, "d1")
        with self.assertRaises(dispatch.StageError):  # reject asks why
            decide.choose(self.cfg, self.c, review["id"], "reject", "user")
        with mock.patch.object(dispatch, "reject", return_value={"state": "archived"}):
            decide.choose(self.cfg, self.c, review["id"], "reject", "user", note="wrong cohort")
        left = [d for d in decide.rows(self.c, "d1", open_only=False) if d["kind"] == "plan"]
        self.assertEqual([d["void_reason"] for d in left], ["the draft was rejected"])


if __name__ == "__main__":
    unittest.main()
