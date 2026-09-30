"""Lifecycle stages from the record, never guessed: a dispatch's phase follows what happened to it (offered to the
planner, plan refused or written, held, replanned, run, reconciled, archived), each decision is asked in the stage
that owns it, and each stage counts its own unit (tickets before grouping, every dispatch after)."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import cli, db, decide, dispatch
from factory.config import Config, Context

SNAP = "2026-09-01T00:00:00Z"
PLAN = [{"id": "root", "result": "r"}, {"id": "FIN-1", "result": "r"}, {"id": "FIN-1/1", "title": "a"}]
RUN = ("staged", "executing")


def raw(ident, domain="API"):
    return json.dumps({"identifier": ident, "title": f"Fix {ident}", "url": f"https://linear.app/j/issue/{ident}/fix",
                       "state": {"name": "Todo", "type": "unstarted"}, "team": {"key": "FIN", "id": "team"},
                       "assignee": None, "priority": 2, "description": f"Domain: {domain}", "labels": {"nodes": []}})


def run_cli(fn, cfg, conn, a):
    """A command's JSON output."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(cfg, conn, a)
    return json.loads(buf.getvalue())


class Phases(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.c = db.connect(self.tmp / "t.db")
        self.addCleanup(self.c.close)
        self.c.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1',?,?,'unstarted',1,?)",
                       (SNAP, SNAP, raw("FIN-1")))
        self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,repo,kind,reason,evidence_json,created_at,"
                       "created_by) VALUES ('i1',?,'r1','valid','r','[1]',?,'t')", (SNAP, SNAP))
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at,drafted_by) "
                       "VALUES ('d1','draft','[]','x',?,'user')", (SNAP,))
        self.c.execute("INSERT INTO dispatch_ticket(run_id,issue_id,identifier,snapshot_updated_at,verdict_id) "
                       "VALUES ('d1','i1','FIN-1',?,1)", (SNAP,))
        self.cfg = SimpleNamespace(repos={}, raw={}, dispatches=self.tmp, mirror_path=lambda r: self.tmp / r)

    def phase(self, run_id="d1"):
        return cli.dispatch_status(self.cfg, self.c, run_id)["phase"]

    def planning(self):
        return tuple(self.c.execute("SELECT planning_requested_at, planning_error FROM dispatch WHERE run_id='d1'")
                     .fetchone())

    def gate(self):
        with mock.patch.object(dispatch, "_tickets_for_render", return_value=([], {})):
            return run_cli(cli.cmd_draft, self.cfg, self.c, SimpleNamespace(dcmd="gate"))

    def plan(self, steps: str):
        return run_cli(cli.cmd_draft, self.cfg, self.c, SimpleNamespace(dcmd="plan", run_id="d1", steps=steps))

    def test_phase_follows_the_record_through_offer_refusal_plan_hold_replan_and_run(self):
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
                       "VALUES ('d2','draft','[]','x','2026-09-02T00:00:00Z')")
        self.assertEqual((self.phase(), self.planning()), ("draft", (None, None)))  # nothing offered yet
        offer = self.gate()["context"]["draft"]  # the planner is handed the offer as it was committed
        offered = self.planning()[0]
        self.assertIsNotNone(offered)
        self.assertEqual((offer["run_id"], offer["phase"], offer["planning_requested_at"]),
                         ("d1", self.phase(), offered))
        self.assertEqual((self.phase("d1"), self.phase("d2")), ("plan", "draft"))  # one offer per gate tick
        for steps in ("[{", json.dumps(PLAN[::2])):  # the planner's refusals: not JSON, FIN-1 without its result
            with self.subTest(steps):
                with self.assertRaises(dispatch.StageError) as refused:
                    self.plan(steps)
                self.assertEqual((self.phase(), self.planning()), ("plan", (offered, str(refused.exception))))
        self.plan(json.dumps(PLAN))
        self.assertEqual((self.phase(), self.planning()[1]), ("review", None))  # a written plan clears the refusal
        dispatch.hold(self.c, "d1", "look first", "user")
        got = cli.dispatch_status(self.cfg, self.c, "d1")
        self.assertEqual((got["phase"], got["review"]), ("review", "held"))
        dispatch.replan(self.c, "d1", "split it", "user")
        self.assertEqual(self.phase(), "plan")  # an explicit request, before the gate's next tick
        self.assertGreater(self.planning()[0], offered)
        self.plan(json.dumps(PLAN))
        self.assertEqual(self.phase(), "review")
        self.c.execute("UPDATE dispatch SET state='staged', body_sha256='h', approved_by='u' WHERE run_id='d1'")
        self.assertEqual(self.phase(), "run")
        for state, want in (("executing", "run"), ("done", "reconcile"), ("reconciled", "reconcile"),
                            ("archived", "archive")):
            self.c.execute("UPDATE dispatch SET state=? WHERE run_id='d1'", (state,))
            self.assertEqual(self.phase(), want, state)

    def test_a_draft_planned_while_the_gate_gathers_its_context_is_not_offered(self):
        def planned_meanwhile(*_, **__):
            self.c.execute("UPDATE dispatch SET planned_at=? WHERE run_id='d1'", (SNAP,))
            return [], {}
        with mock.patch.object(dispatch, "_tickets_for_render", side_effect=planned_meanwhile):
            got = run_cli(cli.cmd_draft, self.cfg, self.c, SimpleNamespace(dcmd="gate"))
        self.assertEqual((got, self.phase(), self.planning()),
                         ({"wakeAgent": False, "context": {"draft": None}}, "review", (None, None)))

    def test_a_refused_plan_is_planning_even_without_an_offer(self):
        with self.assertRaises(dispatch.StageError) as refused:  # a plan submitted by hand, no gate before it
            self.plan("[{")
        self.assertEqual((self.phase(), self.planning()), ("plan", (None, str(refused.exception))))

    def test_upgrade_records_no_offer_an_old_draft_never_had(self):
        path = self.tmp / "v19.db"
        c = db.connect(path)
        for col in ("planning_requested_at", "planning_error"):  # back to the v19 dispatch table
            c.execute(f"ALTER TABLE dispatch DROP COLUMN {col}")
        c.execute("PRAGMA user_version=19")
        c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
                  "VALUES ('old','draft','[]','x',?)", (SNAP,))
        c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at,planned_at) "
                  "VALUES ('planned','draft','[]','x',?,?)", (SNAP, SNAP))
        c.close()
        c = db.connect(path)
        self.addCleanup(c.close)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 20)
        got = {r: cli.dispatch_status(self.cfg, c, r) for r in ("old", "planned")}
        self.assertEqual({r: (d["phase"], d["planning_requested_at"], d["planning_error"]) for r, d in got.items()},
                         {"old": ("draft", None, None), "planned": ("review", None, None)})

    def test_each_decision_is_asked_in_the_stage_that_owns_it(self):
        opts = [decide.option("a", "A", "leads to a"), decide.option("b", "B", "leads to b")]
        for kind in decide.PHASE:
            decide.open_(self.c, kind, f"{kind}?", opts, "a", "why", "t", run_id="d1", node_id="FIN-1", issue_id="i1")
        want = {"ask": "run", "executor-gone": "run", "dispatch-stuck": "run", "plan": "review", "review": "review",
                "writeback": "reconcile", "blocked": "draft", "learning": "learn"}
        self.assertEqual({d["kind"]: d["phase"] for d in cli.dispatch_status(self.cfg, self.c, "d1")["decisions"]},
                         want)
        self.assertEqual({e["detail"]["decision"]: e["detail"]["phase"] for e in cli.ticket_timeline(self.c, "i1")
                          if e["kind"] == "decision"}, want)


class Overview(unittest.TestCase):
    """FIN-1 unverified, FIN-2 needs an answer, FIN-3 stageable, FIN-4 in a draft offered to the planner, FIN-5
    another lead's; dispatches in every stage, six of them archived (five rejected drafts)."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        tmp = Path(d.name)
        self.c = c = db.connect(tmp / "t.db")
        self.addCleanup(c.close)
        self.cfg = Config(raw={"linear": {"lead": "me@x"}}, db=tmp / "t.db", mirrors=tmp, dispatches=tmp,
                          contexts=[Context("api", "o/api", domains=["API"], route="captain")], repos={}, witnesses={})
        holds = mock.patch.object(dispatch, "_foreign_holds", return_value={})  # no herdr in tests
        holds.start()
        self.addCleanup(holds.stop)
        c.executemany("INSERT INTO linear_project VALUES (?,?,?,?,?)",
                      [("p1", "aaaaaaaaaaaa", "API", "me@x", SNAP), ("p2", "bbbbbbbbbbbb", "Other", "other@x", SNAP)])
        for i in range(1, 6):
            c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,'unstarted',1,?)",
                      (f"i{i}", f"FIN-{i}", SNAP, SNAP, raw(f"FIN-{i}", "Other" if i == 5 else "API")))
        for i, kind in ((2, "needs-clarification"), (3, "valid"), (4, "valid")):  # verdict ids 1, 2, 3
            c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,context,repo,kind,reason,evidence_json,"
                      "created_at,created_by) VALUES (?,?,'api','o/api',?,'r','[1]',?,'t')",
                      (f"i{i}", SNAP, kind, db.now()))
        # the executing one runs last: one dispatch executes at a time
        runs = {"draft": (), "plan": (), "review": (), "done": (*RUN, "done"),
                "reconciled": (*RUN, "done", "reconciled"), "closed": (*RUN, "done", "reconciled"),
                "staged": ("staged",), "executing": RUN, **{f"gone{i}": () for i in range(1, 6)}}
        for run, states in runs.items():
            c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
                      "VALUES (?,'draft','[]','t',?)", (run, SNAP))
            for state in states:
                c.execute("UPDATE dispatch SET state=?, body_sha256='h', approved_by='u' WHERE run_id=?", (state, run))
        c.execute("UPDATE dispatch SET planning_requested_at=? WHERE run_id='plan'", (SNAP,))
        c.execute("UPDATE dispatch SET planned_at=? WHERE run_id='review'", (SNAP,))
        c.execute("INSERT INTO dispatch_ticket(run_id,issue_id,identifier,snapshot_updated_at,verdict_id) "
                  "VALUES ('plan','i4','FIN-4',?,3)", (SNAP,))
        c.execute("UPDATE dispatch SET state='archived', archived_at='2026-09-01T00:00:00Z' WHERE run_id='closed'")
        for i in range(1, 6):
            c.execute("UPDATE dispatch SET state='archived', body_sha256='h', rejected_reason='no', archived_at=? "
                      "WHERE run_id=?", (f"2026-09-0{i + 1}T00:00:00Z", f"gone{i}"))
        for run, issue, op, decision, status in (
                ("done", "i1", "description", "apply", "confirmed"),              # landed
                ("done", "i1", "state", "skip", "planned"),                       # writes nothing
                ("done", "i3", "state", "apply", "sent"),                         # outcome unknown
                ("followup-20260901-000000", "i3", "create", "apply", "failed"),
                ("sweep-20260901-000000", "i2", "comment", "apply", "planned"),
                ("sweep-20260901-000000", "i2", "state", "flag", "confirmed"),    # held, its decision open
                ("sweep-20260901-000000", "i1", "state", "flag", "confirmed")):   # held, answered
            c.execute("INSERT INTO writeback(run_id,issue_id,op,payload_json,decision,rule,status) "
                      "VALUES (?,?,?,'{}',?,'r',?)", (run, issue, op, decision, status))
        self.held = decide.writeback(c, "sweep-20260901-000000", "i2", "state", {}, "assigned to someone else")
        answered = decide.writeback(c, "sweep-20260901-000000", "i1", "state", {}, "assigned to someone else")
        decide.choose(self.cfg, c, answered, "skip", "user")

    def test_ticket_rows_carry_their_backend_phase(self):
        rows = run_cli(cli.cmd_tickets, self.cfg, self.c, SimpleNamespace(all=True))
        self.assertEqual({t["identifier"]: t["phase"] for t in rows},
                         {"FIN-1": "verify", "FIN-2": "verify", "FIN-3": "draft", "FIN-4": "plan", "FIN-5": "tickets"})

    def test_counts_are_tickets_before_grouping_and_every_dispatch_after(self):
        got = run_cli(cli.cmd_overview, self.cfg, self.c, None)
        self.assertEqual(got["lifecycle"], {"counts": {"tickets": 5, "verify": 2, "draft": 1, "plan": 1, "review": 1,
                                                       "run": 2, "reconcile": 2, "archive": 6}})

    def test_writebacks_are_every_unsettled_write_sweeps_and_followups_too(self):
        got = run_cli(cli.cmd_overview, self.cfg, self.c, None)
        self.assertEqual([(w["run_id"], w["identifier"], w["op"], w["status"], w["decision"], w["decision_id"])
                          for w in got["writebacks"]], [
            ("done", "FIN-3", "state", "sent", "apply", None),
            ("followup-20260901-000000", "FIN-3", "create", "failed", "apply", None),
            ("sweep-20260901-000000", "FIN-2", "comment", "planned", "apply", None),
            ("sweep-20260901-000000", "FIN-2", "state", "confirmed", "flag", self.held)])

    def test_archive_is_every_archived_dispatch_newest_first(self):
        got = run_cli(cli.cmd_status, self.cfg, self.c, SimpleNamespace(archived=True, run_id=None))
        self.assertEqual([(d["run_id"], d["phase"], d["rejected_reason"]) for d in got],
                         [*((f"gone{i}", "archive", "no") for i in range(5, 0, -1)), ("closed", "archive", None)])


if __name__ == "__main__":
    unittest.main()
