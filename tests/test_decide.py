"""Decisions: always a real choice with a recommendation, answered once, and the review's answer carries the
planner's open questions with it. Asking less: what is taken without asking, and what reaches the user when."""
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import ask, cli, db, decide, dispatch

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

    def test_replan_clears_the_plan_and_the_gate_takes_it_again(self):
        self.plan()
        self.c.execute("INSERT INTO dispatch_note(run_id,node_id,author,body,at) VALUES ('d1','FIN-1/2','u','use x',?)",
                       (SNAP,))
        q = next(d for d in decide.rows(self.c, "d1") if d["kind"] == "plan")
        ask.new(self.c, q["id"], "why real?", "user", spawn=lambda *a, **k: None)
        with self.assertRaises(dispatch.StageError):  # needs a reason
            dispatch.replan(self.c, "d1", " ", "user")
        res = dispatch.replan(self.c, "d1", "split FIN-1 into two PRs", "user")
        self.assertEqual((res["steps_cleared"], res["decisions_voided"]), (3, 2))
        self.assertEqual(self.c.execute("SELECT count(*) FROM dispatch_step WHERE run_id='d1'").fetchone()[0], 0)
        self.assertEqual({d["void_reason"] for d in decide.rows(self.c, "d1", open_only=False)}, {"replanned"})
        self.assertEqual(self.c.execute("SELECT status, error FROM ask").fetchone()[:], ("failed", "replanned"))
        d = self.c.execute("SELECT planned_at, held_reason FROM dispatch WHERE run_id='d1'").fetchone()
        self.assertEqual(d[:], (None, None))
        with self.assertRaises(dispatch.StageError):  # nothing to replan until the planner writes again
            dispatch.replan(self.c, "d1", "again", "user")
        cfg = SimpleNamespace(dispatches=Path(tempfile.mkdtemp()), mirror_path=lambda r: Path("/m") / r)
        with mock.patch("builtins.print") as p, \
                mock.patch.object(dispatch, "_tickets_for_render", return_value=([], {})):
            cli.cmd_draft(cfg, self.c, SimpleNamespace(dcmd="gate"))
        gate = json.loads(p.call_args[0][0])
        self.assertTrue(gate["wakeAgent"])
        self.assertEqual(gate["context"]["draft"]["run_id"], "d1")
        self.assertEqual([n["body"] for n in gate["context"]["draft"]["tree"][0]["notes"]],
                         ["Replan: split FIN-1 into two PRs"])
        self.assertEqual([n["body"] for n in gate["context"]["draft"]["earlier_notes"]], ["use x"])
        self.plan()  # the planner writes a new plan
        self.assertEqual(len(decide.rows(self.c, "d1")), 2)


class AskingLess(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
                       "VALUES ('d1','draft','[]','x',?)", (SNAP,))
        self.cfg = SimpleNamespace(repos={}, raw={"notify": {"interrupts_per_day": 1, "digest": ["09:00"]}})

    def blocked(self):
        return decide.blocked(self.c, "d1", "FIN-1", "i1", "no route", "executor")

    def test_nothing_to_weigh_is_taken_without_asking_and_reported(self):
        did = decide.writeback(self.c, "d1", "i1", "state", {"state": "Done"}, "assigned to someone@else")
        self.assertEqual(decide.one(self.c, did)["tier"], "auto")
        [res] = decide.sweep(self.cfg, self.c)
        self.assertEqual((res["id"], res["chosen"]), (did, "skip"))
        digest = decide.notify(self.cfg, self.c, now=datetime.now(UTC))[-1]
        self.assertIn(f"Done for you: #{did}", digest)

    def test_first_executor_crash_is_restarted_a_second_one_is_pushed(self):
        first = decide.executor(self.c, "d1", "executor-gone", "pane gone", 6)
        second = decide.executor(self.c, "d1", "executor-gone", "pane gone again", 6)
        self.assertEqual([decide.one(self.c, d)["tier"] for d in (first, second)], ["auto", "now"])

    def test_five_straight_stars_earn_it_and_one_override_takes_even_silence_away(self):
        asked = [self.blocked() for _ in range(6)]
        self.assertEqual({decide.one(self.c, d)["tier"] for d in asked}, {"digest"})
        for did in asked[:5]:
            decide.choose(self.cfg, self.c, did, "writeback", "user:dashboard")
        self.assertEqual(decide.one(self.c, self.blocked())["tier"], "auto")
        decide.choose(self.cfg, self.c, asked[5], "retry", "user:dashboard", note="try the other fleet")
        after = decide.one(self.c, self.blocked())
        self.assertEqual(after["tier"], "digest")
        self.assertIn("overrode", after["on_timeout"])  # silence no longer takes ★ on this kind

    def test_pushes_are_capped_per_day_and_the_rest_wait_for_the_digest(self):
        ask = lambda: decide.open_(self.c, "ask", "Which seed?", OPTS[:1] + [decide.option("c", "C", "leads to c")],
                                   "a", "because", "executor", run_id="d1")
        today = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        first = ask()
        msgs = decide.notify(self.cfg, self.c, now=today)
        self.assertIn("needs you now", msgs[0])  # the push, then the 09:00 digest listing it too
        self.assertIn(f"#{first}", msgs[1])
        second = ask()
        self.assertEqual(decide.notify(self.cfg, self.c, now=today + timedelta(minutes=10)), [])  # cap reached
        self.assertIsNone(decide.one(self.c, second)["notified_at"])
        tomorrow = decide.notify(self.cfg, self.c, now=today + timedelta(days=1))
        self.assertTrue(any(f"#{second}" in m for m in tomorrow))

    def test_a_factory_drafts_review_is_pushed_once_planned_not_at_the_next_digest(self):
        self.c.execute("UPDATE dispatch SET drafted_by=?, planned_at=? WHERE run_id='d1'", (dispatch.PROPOSE, SNAP))
        did = decide.review(self.c, "d1", "approve", "small and safe", "agent:factory-plan")
        evening = datetime.now().astimezone().replace(hour=22, minute=0, second=0, microsecond=0)
        [push, *_] = decide.notify(self.cfg, self.c, now=evening)  # evening: the 09:00 digest is long gone
        self.assertTrue(push.startswith(decide.READY) and f"#{did}" in push)
        self.assertIsNotNone(decide.one(self.c, did)["due_at"])  # silence clock starts now, not at 09:00
        self.assertEqual(decide.notify(self.cfg, self.c, now=evening + timedelta(minutes=10)), [])  # once


if __name__ == "__main__":
    unittest.main()
