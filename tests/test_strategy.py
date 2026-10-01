"""Strategy work briefs: versioned intent, immutable publication, readiness and source capture."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factory import db, strategy
from factory.config import Config, Context
from factory.dispatch import StageError

SNAP = "2026-09-01T00:00:00Z"


def _raw(ident="FIN-1", title="t", state="unstarted", assignee=None):
    return json.dumps({"title": title, "url": f"https://linear/{ident}", "identifier": ident,
                       "description": "Domain: My Domain\n", "state": {"name": state},
                       "assignee": assignee, "priority": 3, "labels": {"nodes": []}, "team": {"key": "TEAM"}})


def _body(**over):
    b = {"title": "T", "outcome": "do it", "acceptance": ["a"], "scope": ["s"], "exclusions": [],
         "decisions": [], "dependencies": [], "resources": [], "risks": [], "evidence": []}
    b.update(over)
    return b


class Briefs(unittest.TestCase):
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
        for ident in ("FIN-1", "FIN-2"):
            self.c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,'unstarted',1,?)",
                           (ident.lower(), ident, SNAP, SNAP, _raw(ident=ident)))
            self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,context,repo,kind,reason,evidence_json,"
                           "created_at,created_by) VALUES (?,?,'ctx','Finks-ai/finks-ddd','valid','r','[\"e\"]',?,'t')",
                           (ident.lower(), SNAP, SNAP))
        self.c.execute("INSERT INTO repo_trunk VALUES ('Finks-ai/finks-ddd','main','sha',?)", (SNAP,))

    def test_create_draft_defaults_global_resource(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli")
        self.assertEqual(b["state"], "draft")
        self.assertEqual(b["revision"], 1)
        self.assertIsNone(b["parent_id"])
        self.assertEqual(b["body"]["resources"],
                         ["repo:Finks-ai/finks-ddd", "route:fx-news", "global:*"])
        self.assertEqual([s["identifier"] for s in b["sources"]], ["FIN-1"])

    def test_create_with_reviewed_resources_drops_global(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body(resources=["clickhouse:serving/ck_dev"]))
        self.assertEqual(b["body"]["resources"],
                         ["repo:Finks-ai/finks-ddd", "route:fx-news", "clickhouse:serving/ck_dev"])

    def test_create_rejects_unknown_fields_and_self_dependency(self):
        with self.assertRaises(StageError):
            strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body(**{"bogus": 1}))
        with self.assertRaises(StageError):
            strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body(dependencies=["FIN-1"]))

    def test_approve_freezes_and_requires_complete_body(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli")  # scaffold: empty acceptance/scope
        with self.assertRaises(StageError):
            strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        strategy.revise(self.cfg, self.c, b["id"], _body(), None, "user:cli")
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        self.assertEqual(strategy.get(self.c, b["id"])["state"], "approved")
        # a published brief is never edited in place: revise appends a new draft revision instead
        b2 = strategy.revise(self.cfg, self.c, b["id"], _body(title="X"), "change", "user:cli")
        self.assertEqual(b2["revision"], 2)
        self.assertEqual(b2["parent_id"], b["id"])
        self.assertEqual(strategy.get(self.c, b["id"])["body"]["title"], "T")
        # raw sqlite3 cannot overwrite a published body either
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("UPDATE work_brief SET body_json=? WHERE id=?", (json.dumps(_body(title="Z")), b["id"]))

    def test_amend_appends_a_new_draft_revision(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        with self.assertRaises(StageError):  # amendment reason required
            strategy.revise(self.cfg, self.c, b["id"], _body(title="T2"), "", "user:cli")
        b2 = strategy.revise(self.cfg, self.c, b["id"], _body(title="T2"), "source changed", "user:cli")
        self.assertEqual(b2["state"], "draft")
        self.assertEqual(b2["revision"], 2)
        self.assertEqual(b2["parent_id"], b["id"])
        self.assertEqual(b2["amendment_reason"], "source changed")
        self.assertEqual(strategy.get(self.c, b["id"])["state"], "approved")  # parent unchanged

    def test_hold_unhold_preserves_intent_and_audits(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        strategy.hold(self.cfg, self.c, b["id"], "wait for X", "user:cli")
        self.assertEqual(strategy.get(self.c, b["id"])["state"], "held")
        # hold preserves intent/version: the body is unchanged (resources merged at create, as approved)
        self.assertEqual(strategy.get(self.c, b["id"])["body"]["outcome"], "do it")
        strategy.unhold(self.cfg, self.c, b["id"], "user:cli")
        self.assertEqual(strategy.get(self.c, b["id"])["state"], "approved")
        audit = [tuple(r) for r in self.c.execute("SELECT action FROM work_brief_hold ORDER BY id")]
        self.assertEqual(audit, [("hold",), ("unhold",)])

    def test_ready_blocks_held_and_source_change_and_dependency(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli",
                            _body(dependencies=["FIN-2"]))
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        (row,) = strategy.ready(self.cfg, self.c)
        self.assertFalse(row["ready"])
        self.assertIn("dependency FIN-2 is unmet", row["blockers"])
        # the dependency is recorded completed -> met
        self.c.execute("INSERT INTO linear_snapshot VALUES ('fin-2','FIN-2',?,?,'completed',1,?)",
                       ("2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z", _raw(ident="FIN-2", state="completed")))
        (row,) = strategy.ready(self.cfg, self.c)
        self.assertTrue(row["ready"])
        self.assertEqual(row["blockers"], [])
        # source changed since capture -> needs-amendment, no longer ready
        self.c.execute("INSERT INTO linear_snapshot VALUES ('fin-1','FIN-1',?,?,'unstarted',1,?)",
                       ("2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z", _raw(ident="FIN-1")))
        (row,) = strategy.ready(self.cfg, self.c)
        self.assertFalse(row["ready"])
        self.assertIn("source changed since capture: FIN-1", row["blockers"])

    def test_ready_excludes_consumed_brief(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at,brief_id) "
                       "VALUES ('d1','draft','[]','x',?,?)", (SNAP, b["id"]))
        self.assertEqual(strategy.ready(self.cfg, self.c), [])

    def test_dependency_cycle_rejected(self):
        a = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body(dependencies=["FIN-2"]))
        self.assertIsNotNone(a)
        with self.assertRaises(StageError):
            strategy.create(self.cfg, self.c, ["FIN-2"], "user:cli", _body(dependencies=["FIN-1"]))

    def test_render_and_ticket_context_use_compiled_intent(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body(outcome="the outcome"))
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        md = strategy.render(self.c, b["id"])
        self.assertIn("the outcome", md)
        self.assertIn("Finks-ai/finks-ddd", md)
        self.assertNotIn("https://linear/FIN-1/description", md)  # not a raw narrative dump
        tc = strategy.ticket_context(self.c, b["id"], "FIN-1")
        self.assertEqual(tc["description"], md)
        self.assertEqual(tc["repo"], "Finks-ai/finks-ddd")
        self.assertEqual(tc["identifier"], "FIN-1")

    def test_groom_mocked_produces_draft_with_global_default(self):
        model = _body(resources=["clickhouse:serving/ck_dev"])  # the model's resource is never trusted
        with mock.patch.object(strategy, "_run_groom", return_value=model):
            b = strategy.groom(self.cfg, self.c, ["FIN-1"], "user:cli")
        self.assertEqual(b["state"], "draft")
        self.assertEqual(b["body"]["resources"],
                         ["repo:Finks-ai/finks-ddd", "route:fx-news", "global:*"])

    def test_dispatch_pins_one_brief_version(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at,brief_id) "
                       "VALUES ('d1','draft','[]','x',?,?)", (SNAP, b["id"]))
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at,brief_id) "
                           "VALUES ('d2','draft','[]','x',?,?)", (SNAP, b["id"]))

    def test_dispatch_resources_immutable_after_stage(self):
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
                       "VALUES ('d1','draft','[]','x',?)", (SNAP,))
        self.c.execute("INSERT INTO dispatch_resource VALUES ('d1','global:*')")
        self.c.execute("UPDATE dispatch SET state='staged', body_sha256='h', approved_by='u' WHERE run_id='d1'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("INSERT INTO dispatch_resource VALUES ('d1','repo:x')")
        with self.assertRaises(sqlite3.IntegrityError):  # held until archived
            self.c.execute("DELETE FROM dispatch_resource WHERE run_id='d1'")


if __name__ == "__main__":
    unittest.main()
