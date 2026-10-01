"""Strategy relationship capture: drift via semantic fingerprint, frozen render, groom candidates, warnings."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factory import db, strategy
from factory.config import Config, Context
from factory.dispatch import StageError

SNAP = "2026-09-01T00:00:00Z"


def _raw(ident="FIN-1", title="t"):
    return json.dumps({"title": title, "url": f"https://linear/{ident}", "identifier": ident,
                       "description": "Domain: My Domain\n", "state": {"name": "unstarted"},
                       "assignee": None, "priority": 3, "labels": {"nodes": []}, "team": {"key": "TEAM"}})


def _body(**over):
    b = {"title": "T", "outcome": "do it", "acceptance": ["a"], "scope": ["s"], "exclusions": [],
         "decisions": [], "dependencies": [], "resources": [], "risks": [], "evidence": []}
    b.update(over)
    return b


def _rel(fingerprint="fp1", complete=True, observed_at=SNAP, edges=(), nodes=()):
    return {"complete": complete, "observed_at": observed_at, "fingerprint": fingerprint,
            "edges": list(edges), "nodes": list(nodes)}


def _node(ident, title=None):
    return {"identifier": ident, "title": title or ident, "url": "", "state": None, "state_type": None,
            "assignee": None, "project": None}


class RelationshipBriefs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = db.connect(Path(self.tmp.name) / "f.db")
        self.addCleanup(self.c.close)
        self.cfg = Config(raw={"linear": {"lead": "me@example.com", "team": {}}},
                          db=Path(self.tmp.name) / "f.db", mirrors=Path(self.tmp.name) / "m",
                          dispatches=Path(self.tmp.name) / "d",
                          contexts=[Context(name="ctx", repo="Finks-ai/finks-ddd", domains=["My Domain"],
                                            route="fx-news")],
                          repos={}, witnesses={})
        self.c.execute("INSERT INTO linear_project VALUES ('p1','my-domain','My Domain','me@example.com',?)",
                       (SNAP,))
        for ident in ("FIN-1", "FIN-2"):
            self.c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,'unstarted',1,?)",
                           (ident.lower(), ident, SNAP, SNAP, _raw(ident=ident)))
            self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,context,repo,kind,reason,evidence_json,"
                           "created_at,created_by) VALUES (?,?,'ctx','Finks-ai/finks-ddd','valid','r','[\"e\"]',?,'t')",
                           (ident.lower(), SNAP, SNAP))
        self.c.execute("INSERT INTO repo_trunk VALUES ('Finks-ai/finks-ddd','main','sha',?)", (SNAP,))
        self.snapshots = {}
        patcher = mock.patch.object(strategy.relationships, "snapshot", side_effect=self._snap)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _snap(self, conn, ident):
        return self.snapshots.get(ident, _rel(fingerprint=None, complete=False, observed_at=None))

    def test_relationship_drift_prevents_approve(self):
        self.snapshots["FIN-1"] = _rel()
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())  # captures fp1
        self.snapshots["FIN-1"] = _rel(fingerprint="fp2")
        with self.assertRaises(StageError):
            strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")

    def test_relationship_drift_invalidates_approved_readiness(self):
        self.snapshots["FIN-1"] = _rel()
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        self.snapshots["FIN-1"] = _rel(fingerprint="fp2")
        (row,) = strategy.ready(self.cfg, self.c)
        self.assertFalse(row["intent_ready"])
        self.assertEqual(row["source_changed"], ["FIN-1"])

    def test_timestamp_refresh_stable(self):
        self.snapshots["FIN-1"] = _rel(observed_at="2026-09-01T00:00:00Z")
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        self.snapshots["FIN-1"] = _rel(observed_at="2026-09-02T00:00:00Z")  # same fingerprint, later observed_at
        result = strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        self.assertEqual(result["state"], "approved")

    def test_complete_becoming_incomplete_invalidates(self):
        self.snapshots["FIN-1"] = _rel()
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        self.snapshots["FIN-1"] = _rel(complete=False, fingerprint="fp1")  # fingerprint still matches
        self.assertTrue(strategy._source_changed(self.c, b["sources"][0]))

    def test_ticket_change_still_invalidates_relationship_captured_brief(self):
        self.snapshots["FIN-1"] = _rel()
        brief = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        strategy.approve(self.cfg, self.c, brief["id"], "user:dashboard")
        self.c.execute("INSERT INTO linear_snapshot VALUES ('fin-1','FIN-1',?,?,'unstarted',1,?)",
                       ("2026-09-02T00:00:00Z", SNAP, _raw()))
        (ready,) = strategy.ready(self.cfg, self.c)
        self.assertFalse(ready["intent_ready"])
        self.assertEqual(ready["source_changed"], ["FIN-1"])

    def test_render_uses_captured_relationships_immutable(self):
        self.snapshots["FIN-1"] = _rel(edges=[{"kind": "parent", "source": "EP-1", "target": "FIN-1"}],
                                       nodes=[_node("EP-1", "Epic One")])
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        md = strategy.render(self.c, b["id"])
        self.assertIn("parent: EP-1 → FIN-1", md)
        self.assertIn("EP-1: Epic One", md)
        # a later relation refresh never rewrites the frozen render
        self.snapshots["FIN-1"] = _rel(fingerprint="fp2",
                                       edges=[{"kind": "parent", "source": "EP-2", "target": "FIN-1"}],
                                       nodes=[_node("EP-2", "Epic Two")])
        self.assertEqual(strategy.render(self.c, b["id"]), md)
        self.assertNotIn("EP-2", strategy.render(self.c, b["id"]))

    def test_legacy_render_unchanged(self):
        legacy = strategy._sources_for(self.cfg, self.c, ["FIN-1"])
        legacy[0].pop("relationships")
        with mock.patch.object(strategy, "_sources_for", return_value=legacy):
            b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        md = strategy.render(self.c, b["id"])
        self.assertNotIn("## Relationships", md)
        self.assertIn("- FIN-1: t (Finks-ai/finks-ddd; context ctx)", md)
        self.assertIn("snapshot 2026-09-01T00:00:00Z, verdict valid", md)
        frozen = strategy.get(self.c, b["id"])["sources"]
        self.snapshots["FIN-1"] = _rel(fingerprint="later", edges=[
            {"kind": "blocks", "source": "FIN-2", "target": "FIN-1"}])
        self.assertEqual(strategy.render(self.c, b["id"]), md)
        self.assertEqual(strategy.get(self.c, b["id"])["sources"], frozen)
        self.assertTrue(strategy.ready(self.cfg, self.c)[0]["intent_ready"])

    def test_groom_rejects_non_candidate_dependency(self):
        self.snapshots["FIN-1"] = _rel()  # no recorded blocks edge -> no candidates
        with mock.patch.object(strategy, "_run_groom", return_value=_body(dependencies=["FIN-2"])):
            with self.assertRaises(StageError):
                strategy.groom(self.cfg, self.c, ["FIN-1"], "user:cli")

    def test_groom_rejects_own_id_dependency(self):
        self.snapshots["FIN-1"] = _rel()  # own id is never a candidate
        with mock.patch.object(strategy, "_run_groom", return_value=_body(dependencies=["FIN-1"])):
            with self.assertRaises(StageError):
                strategy.groom(self.cfg, self.c, ["FIN-1"], "user:cli")

    def test_groom_accepts_recorded_candidate(self):
        self.snapshots["FIN-1"] = _rel(edges=[{"kind": "blocks", "source": "FIN-2", "target": "FIN-1"}],
                                       nodes=[_node("FIN-2")])
        with mock.patch.object(strategy, "_run_groom", return_value=_body(dependencies=["FIN-2"])):
            b = strategy.groom(self.cfg, self.c, ["FIN-1"], "user:cli")
        self.assertEqual(b["state"], "draft")
        self.assertEqual(b["body"]["dependencies"], ["FIN-2"])

    def test_human_dependencies_remain_editable(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body(dependencies=["FIN-2"]))
        self.assertEqual(b["body"]["dependencies"], ["FIN-2"])  # no candidate restriction on a human
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        b2 = strategy.revise(self.cfg, self.c, b["id"], _body(dependencies=[]), "drop dep", "user:cli")
        self.assertEqual(b2["body"]["dependencies"], [])

    def test_overview_groups_and_relationships_summary(self):
        self.snapshots["FIN-1"] = _rel(observed_at="2026-09-03T00:00:00Z")
        # FIN-2 has no snapshot -> missing
        ov = strategy.overview(self.cfg, self.c)
        self.assertEqual(ov["relationships"], {"complete": False, "observed_at": "2026-09-03T00:00:00Z",
                                               "missing": ["FIN-2"]})
        self.assertEqual(sorted(m for g in ov["groups"] for m in g["members"]), ["FIN-1", "FIN-2"])



if __name__ == "__main__":
    unittest.main()
