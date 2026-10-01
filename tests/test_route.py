"""Direct routing: a dispatch with a route goes to that domain lead's live pane, else to the captain; only the pane
that runs it may take it (execute)."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import _helpers  # noqa: E402

from factory import db, dispatch, scheduler
from factory.config import Context

SNAP = _helpers.SNAP
LEAD, CAPTAIN = {"pane_id": "w6X:p2", "agent": "omp", "agent_status": "done"}, {"pane_id": "w6M:p1", "agent_status": "idle"}


class Route(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.homes = tmp / "homes"
        (self.homes / "factory-primary" / "state").mkdir(parents=True)
        (self.homes / "fx-news-pipeline" / "state").mkdir(parents=True)
        self.c = db.connect(tmp / "f.db")
        self.cfg = SimpleNamespace(raw={}, dispatches=tmp / "dispatches")
        body = b"# Dispatch d1\n"
        (self.cfg.dispatches / "d1").mkdir(parents=True)
        (self.cfg.dispatches / "d1" / "dispatch.md").write_bytes(body)
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at,route) "
                       "VALUES ('d1','draft','[]','x',?,'fx-news-pipeline')", (SNAP,))
        scheduler.set_claims(self.c, "d1", {"repo:o/api", "route:fx-news-pipeline"})  # pinned while draft
        self.c.execute("UPDATE dispatch SET state='staged', body_sha256=?, approved_by='u', last_actor='p' "
                       "WHERE run_id='d1'", (hashlib.sha256(body).hexdigest(),))
        patches = [mock.patch.object(dispatch, "FLEET_HOMES", self.homes),
                   mock.patch.object(dispatch, "_herdr", return_value={"result": {"panes": [LEAD, CAPTAIN]}}),
                   mock.patch.object(dispatch, "_executor", return_value=CAPTAIN),
                   mock.patch.object(dispatch, "_safety_check")]  # freshness is tested elsewhere
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def spawned(self):
        (self.homes / "factory-primary" / "state" / "fx-news-pipeline.meta").write_text(
            "kind=secondmate\nherdr_pane_id=w6X:p2\n")

    def handoff(self):
        with mock.patch.object(dispatch, "_send") as send:
            res = dispatch.handoff(self.cfg, self.c, "d1")
        return res, send.call_args.args

    def test_a_routed_dispatch_goes_to_its_live_lead_with_a_fresh_session(self):
        self.spawned()
        res, (pane, sent) = self.handoff()
        self.assertEqual((res["via"], pane, sent), ("fx-news-pipeline", "w6X:p2", ["/new", "run dispatch-intake d1"]))

    def test_a_lead_never_spawned_or_still_supervising_crews_is_not_used(self):
        res, (pane, _) = self.handoff()  # no meta: never spawned, the captain spawns it and routes
        self.assertEqual((res["via"], pane), ("factory-primary", "w6M:p1"))
        self.spawned()
        (self.homes / "fx-news-pipeline" / "state" / "home-summary.json").write_text(
            json.dumps({"active_children": ["fx-fin-1"], "decisions_open": []}))
        with self.assertRaisesRegex(dispatch.StageError, "crews or decisions open"):
            self.handoff()

    def test_only_the_lead_pane_or_the_captain_workspace_may_take_it(self):
        self.spawned()
        env = lambda pane: mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": pane.split(":")[0], "HERDR_PANE_ID": pane})
        other = SimpleNamespace(returncode=0, stdout=json.dumps({"result": {"workspace": {"label": "2ndmate-fx-core-rs"}}}))
        with env("w6W:p2"), mock.patch.object(dispatch.subprocess, "run", return_value=other), \
                self.assertRaisesRegex(dispatch.StageError, "neither fx-news-pipeline's lead"):
            dispatch.execute(self.cfg, self.c, "d1", "fx-core-rs")
        self.c.execute("INSERT INTO dispatch_launch(run_id,pane_id,state,claimed_at) VALUES ('d1','w6X:p2','sent',?)",
                       (SNAP,))  # handoff reserved the lead pane; execute requires its own launch
        with env("w6X:p2"):
            res = dispatch.execute(self.cfg, self.c, "d1", "fx-news-pipeline")
        self.assertEqual(res["executor_pane"], "w6X:p2")
        self.assertEqual(self.c.execute("SELECT state FROM dispatch").fetchone()[0], "executing")

    def test_a_context_routes_by_domain_where_one_repo_has_two_owners(self):
        ctx = Context(name="finks-data", repo="r", route={"Finks Data": "fx-finks-data", "FMP Ingestion Pipeline": "fx-financial-data"})
        self.assertEqual([ctx.owner("Finks Data"), ctx.owner("FMP Ingestion Pipeline"), ctx.owner("Other")],
                         ["fx-finks-data", "fx-financial-data", None])


if __name__ == "__main__":
    unittest.main()
