"""Decisions: always a real choice with a recommendation, answered once, and the review's answer carries the
planner's open questions with it. Asking less: what is taken without asking, and what reaches the user when."""
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
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
        return dispatch.plan(self.cfg, self.c, "d1", [
            {"id": "root", "title": "theme", "recommend": recommend, "why": "small and safe", "result": "r"},
            {"id": "FIN-1", "result": "r"},
            {"id": "FIN-1/1", "title": "a"}, {"id": "FIN-1/2", "title": "b", "depends_on": ["FIN-1/1"]},
            {"question": "Real engine or mock?", "key": "engine", "now": "tests mock it", "on": "FIN-1/2",
             "recommend": "real", "why": "the bug only shows on the engine", "options": [
                 {"id": "real", "label": "Real engine", "leads_to": "slower CI, catches the bug", "changes": [],
                  "result": "caught"},
                 {"id": "mock", "label": "Mock", "leads_to": "fast, misses the bug", "result": "missed",
                  "changes": [{"step": "FIN-1/2", "becomes": None}]}]}])

    def test_planner_questions_need_options_and_a_recommendation(self):
        opt = lambda i: {"id": i, "label": i, "leads_to": i, "changes": [], "result": i}
        for q in ({"question": "x?", "options": [opt("a")], "recommend": "a", "why": "w"},
                  {"question": "x?", "options": [dict(o) for o in OPTS], "recommend": "a", "why": "w"},  # note key
                  {"question": "x?", "on": "FIN-9/1", "options": [opt("a"), opt("z")], "recommend": "a", "why": "w"}):
            with self.subTest(q=q), self.assertRaises(dispatch.StageError):
                dispatch.plan(self.cfg, self.c, "d1", [{"id": "root", "result": "r"}, {"id": "FIN-1", "result": "r"},
                                                       {"id": "FIN-1/1", "title": "a"}, {**q, "key": "k", "now": "n"}])

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


class Configurator(unittest.TestCase):
    """What the phone configurator shows per choice: predicted results, files, and per option the plan changes."""
    def setUp(self):
        Decisions.setUp(self)
        repo = Path(tempfile.mkdtemp())
        git = lambda *a: dispatch.repos.git(repo, *a)
        git("init", "-q"); git("config", "user.email", "t@x"); git("config", "user.name", "t")
        (repo / "app").mkdir(); (repo / "app" / "cors.py").write_text("ORIGINS = []\n")
        git("add", "."); git("commit", "-qm", "one")
        sha = git("rev-parse", "HEAD").strip()
        self.c.execute("UPDATE dispatch SET repos_json=? WHERE run_id='d1'", (f'[{{"repo":"r1","trunk_sha":"{sha}"}}]',))
        self.cfg = SimpleNamespace(repos={}, raw={}, mirror_path=lambda _: repo, dispatches=Path(tempfile.mkdtemp()))

    def nodes(self, **q2):
        opt = lambda i, **k: {"id": i, "label": i, "leads_to": i, "result": f"{i} lands", "changes": [], **k}
        return [
            {"id": "root", "title": "CORS", "result": "console loads"}, {"id": "FIN-1", "result": "no CORS error"},
            {"id": "FIN-1/1", "title": "allowlist", "files": ["app/cors.py", {"path": "tests/test_cors.py", "new": True}]},
            {"id": "FIN-1/2", "title": "PR"},
            {"question": "Previews too?", "key": "origins", "on": "FIN-1/1", "now": "only prod is listed",
             "evidence": ["app/cors.py:1", {"path": "app/cors.py", "line": 1, "note": "empty list"}],
             "recommend": "prod", "why": "ticket names prod", "options": [
                 opt("prod", cost="0", risk="none"),
                 opt("both", cost="+1 step", risk="wildcard", changes=[
                     {"step": "FIN-1/1", "becomes": "allowlist + previews"},
                     {"add": {"id": "FIN-1/3", "title": "test previews", "depends_on": ["FIN-1/1"],
                              "files": [{"path": "tests/test_previews.py", "new": True}]}}])]},
            {"question": "Wildcard?", "key": "wild", "now": "no wildcard support", "recommend": "no", "why": "safer",
             "depends_on": {"question": "origins", "option": "both"},
             "options": [opt("no"), opt("yes", changes=[{"step": "FIN-1/2", "becomes": None}])], **q2}]

    def test_plan_fields_round_trip_into_the_overview(self):
        from factory import cli
        dispatch.plan(self.cfg, self.c, "d1", self.nodes())
        got = cli.dispatch_status(self.cfg, self.c, "d1")
        tree = {n["id"]: n for n in got["tree"]}
        self.assertEqual((tree["root"]["result"], tree["FIN-1"]["result"], tree["FIN-1/2"]["files"]),
                         ("console loads", "no CORS error", []))
        self.assertEqual(tree["FIN-1/1"]["files"], [{"path": "app/cors.py", "new": False},
                                                    {"path": "tests/test_cors.py", "new": True}])
        origins, wild = [d for d in got["decisions"] if d["kind"] == "plan"]
        self.assertEqual((origins["key"], origins["now"], origins["depends_on"]), ("origins", "only prod is listed", None))
        self.assertEqual(origins["evidence"], [{"path": "app/cors.py", "line": 1},
                                               {"path": "app/cors.py", "line": 1, "note": "empty list"}])
        both = origins["options"][1]
        self.assertEqual((both["result"], both["cost"], both["risk"]), ("both lands", "+1 step", "wildcard"))
        self.assertEqual(both["changes"], [
            {"step": "FIN-1/1", "becomes": "allowlist + previews"},
            {"add": {"id": "FIN-1/3", "title": "test previews", "detail": "", "depends_on": ["FIN-1/1"],
                     "files": [{"path": "tests/test_previews.py", "new": True}]}}])
        self.assertEqual(wild["depends_on"], {"question": "origins", "option": "both"})
        self.assertEqual(wild["options"][0]["cost"], None)

    def test_bad_plan_fields_are_refused(self):
        n = self.nodes()
        bad = {"missing ticket result": [{**x, "result": None} if x.get("id") == "FIN-1" else x for x in n],
               "unknown step in changes": self.nodes(options=[
                   n[5]["options"][0], {**n[5]["options"][1], "changes": [{"step": "FIN-1/9", "becomes": "x"}]}]),
               "added step already exists": self.nodes(options=[
                   n[5]["options"][0], {**n[5]["options"][1], "changes": [{"add": {"id": "FIN-1/2", "title": "x"}}]}]),
               "option without result": self.nodes(options=[n[5]["options"][0], {**n[5]["options"][1], "result": ""}]),
               "question without now": self.nodes(now=""),
               "depends_on unknown key": self.nodes(depends_on={"question": "nope", "option": "both"}),
               "depends_on unknown option": self.nodes(depends_on={"question": "origins", "option": "nope"}),
               "depends_on itself": self.nodes(depends_on={"question": "wild", "option": "no"}),
               "questions depend on each other": [*n[:4], {**n[4], "depends_on": {"question": "wild", "option": "no"}},
                                                  n[5]],
               "file missing at trunk": [*n[:2], {**n[2], "files": ["app/nope.py"]}, *n[3:]],
               "evidence missing at trunk": [*n[:4], {**n[4], "evidence": ["app/nope.py:3"]}, n[5]],
               "three questions": [*n, {**n[5], "key": "third"}]}
        for why, nodes in bad.items():
            with self.subTest(why), self.assertRaises(dispatch.StageError):
                dispatch.plan(self.cfg, self.c, "d1", nodes)
        self.assertIsNone(self.c.execute("SELECT planned_at FROM dispatch").fetchone()[0])

    def test_a_plan_written_before_the_fields_still_reads(self):
        self.c.execute("INSERT INTO dispatch_step(run_id, step_id, title) VALUES ('d1','FIN-1/1','a')")
        decide.open_(self.c, "plan", "old?", OPTS[:1] + [decide.option("c", "C", "c")], "a", "w", "t", run_id="d1")
        step = dispatch.tree(self.c, "d1")[-1]
        self.assertEqual((step["id"], step["result"], step["files"]), ("FIN-1/1", None, []))
        q = decide.rows(self.c, "d1")[0]
        self.assertEqual((q["key"], q["now"], q["evidence"], q["depends_on"]), (None, None, [], None))
        self.assertEqual({k: q["options"][0][k] for k in ("changes", "result", "cost", "risk")},
                         {"changes": [], "result": None, "cost": None, "risk": None})



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
