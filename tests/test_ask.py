""""Why?" threads: one pending ask per decision, the planner's answer or failure recorded, follow-ups resume."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from factory import ask, db, decide, dispatch

SNAP = "2026-09-01T00:00:00Z"


class Asks(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        self.c.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1',?,?,'unstarted',1,?)",
                       (SNAP, SNAP, '{"title": "fix totals"}'))
        self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,repo,kind,reason,evidence_json,created_at,"
                       "created_by) VALUES ('i1',?,'o/r1','valid','bug at a.py:3','[1]',?,'t')", (SNAP, SNAP))
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at,drafted_by) "
                       "VALUES ('d1','draft','[]','x',?,'user')", (SNAP,))
        self.c.execute("INSERT INTO dispatch_ticket(run_id,issue_id,identifier,snapshot_updated_at,verdict_id) "
                       "VALUES ('d1','i1','FIN-1',?,1)", (SNAP,))
        self.did = decide.open_(self.c, "plan", "Split it?", [decide.option("a", "A", "x"), decide.option("b", "B", "y")],
                                "a", "smaller PRs", "agent:factory-plan", run_id="d1")
        self.cfg = SimpleNamespace(mirror_path=lambda repo: Path("/m") / repo.replace("/", "__"))
        self.spawned = []

    def new(self, text="why?"):
        return ask.new(self.c, self.did, text, "user", spawn=lambda argv, **kw: self.spawned.append((argv, kw)))

    def run_(self, aid, result):
        calls = []

        def runner(argv, **kw):
            calls.append((argv, Path(argv[argv.index("--query-file") + 1]).read_text()))
            if isinstance(result, Exception):
                raise result
            return result
        out = ask.run(self.cfg, self.c, aid, runner=runner)
        return out, calls[0]

    def row(self, aid):
        return dict(self.c.execute("SELECT * FROM ask WHERE id=?", (aid,)).fetchone())

    def test_answered_with_session_and_detached_spawn(self):
        aid = self.new()["ask"]
        argv, kw = self.spawned[0]
        self.assertEqual(argv[-3:], ["ask", "run", str(aid)])
        self.assertTrue(kw["start_new_session"])
        _, (cmd, prompt) = self.run_(aid, subprocess.CompletedProcess([], 0, "Because a.py:3 sums twice.\n",
                                                                     "\nsession_id: s-1\n"))
        self.assertEqual(cmd[:7], [dispatch.HERMES, "-p", "planner", "chat", "--oneshot", "-Q", "-t"])
        self.assertEqual(cmd[7], "file")
        self.assertNotIn("--resume", cmd)
        self.assertIn("Split it?", prompt)
        self.assertIn("/m/o__r1", prompt)
        r = self.row(aid)
        self.assertEqual((r["status"], r["answer"], r["session_id"]), ("answered", "Because a.py:3 sums twice.", "s-1"))

    def test_failed_on_timeout_and_nonzero_exit(self):
        aid = self.new()["ask"]
        self.run_(aid, subprocess.TimeoutExpired("hermes", ask.TIMEOUT))
        self.assertEqual(self.row(aid)["status"], "failed")
        self.assertIn("180s", self.row(aid)["error"])
        aid = self.new("again")["ask"]  # a failed ask frees the decision for a retry
        self.run_(aid, subprocess.CompletedProcess([], 1, "", "provider down\nsession_id: s-2"))
        r = self.row(aid)
        self.assertEqual((r["status"], r["session_id"]), ("failed", "s-2"))
        self.assertIn("provider down", r["error"])

    def test_second_ask_refused_while_pending(self):
        self.new()
        with self.assertRaises(dispatch.StageError):
            self.new("another")
        self.assertEqual(len(self.spawned), 1)

    def test_follow_up_resumes_the_session(self):
        aid = self.new()["ask"]
        self.run_(aid, subprocess.CompletedProcess([], 0, "first", "session_id: s-1"))
        aid2 = self.new("and then?")["ask"]
        _, (cmd, prompt) = self.run_(aid2, subprocess.CompletedProcess([], 0, "second", ""))
        self.assertEqual(cmd[cmd.index("--resume") + 1], "s-1")
        self.assertNotIn("Plan tree", prompt)  # the session already has it
        self.assertEqual(self.row(aid2)["session_id"], "s-1")


if __name__ == "__main__":
    unittest.main()
