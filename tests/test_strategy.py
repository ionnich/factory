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
        self.c.execute("INSERT INTO linear_project(id,slug_id,name,lead_email,fetched_at) VALUES "
                       "('p1','my-domain','My Domain','me@example.com',?)", (SNAP,))
        for ident in ("FIN-1", "FIN-2"):
            self.c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,'unstarted',1,?)",
                           (ident.lower(), ident, SNAP, SNAP, _raw(ident=ident)))
            self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,context,repo,kind,reason,evidence_json,"
                           "created_at,created_by) VALUES (?,?,'ctx','Finks-ai/finks-ddd','valid','r','[\"e\"]',?,'t')",
                           (ident.lower(), SNAP, SNAP))
        self.c.execute("INSERT INTO repo_trunk VALUES ('Finks-ai/finks-ddd','main','sha',?)", (SNAP,))

    def verify(self, brief_id, identifier, issue_id):
        """What the prune slice does: supersede the current verdict and bind a fresh valid one to this exact version."""
        self.c.execute("UPDATE verdict SET superseded_at=? WHERE issue_id=? AND superseded_at IS NULL",
                       (SNAP, issue_id))
        self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,context,repo,kind,reason,evidence_json,"
                       "created_at,created_by) VALUES (?,?,'ctx','Finks-ai/finks-ddd','valid','r','[\"e\"]',?,'prune')",
                       (issue_id, SNAP, SNAP))
        vid = self.c.execute("SELECT max(id) FROM verdict").fetchone()[0]
        self.c.execute("INSERT INTO brief_verdict(brief_id,issue_id,verdict_id) VALUES (?,?,?)",
                       (brief_id, issue_id, vid))
        return vid

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
        b2 = strategy.revise(self.cfg, self.c, b["id"], _body(), None, "user:cli")  # new draft id, filled
        strategy.approve(self.cfg, self.c, b2["id"], "user:dashboard")
        self.assertEqual(strategy.get(self.c, b2["id"])["state"], "approved")
        self.assertEqual(strategy.get(self.c, b["id"])["state"], "draft")  # original scaffold unchanged
        # a published brief is never edited in place: revise appends a new draft revision instead
        b3 = strategy.revise(self.cfg, self.c, b2["id"], _body(title="X"), "change", "user:cli")
        self.assertEqual(b3["revision"], 3)
        self.assertEqual(b3["parent_id"], b2["id"])
        self.assertEqual(strategy.get(self.c, b2["id"])["body"]["title"], "T")
        # raw sqlite3 cannot overwrite a published body either
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("UPDATE work_brief SET body_json=? WHERE id=?", (json.dumps(_body(title="Z")), b2["id"]))

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
        self.assertIn("source FIN-1 has no verdict for this version", row["blockers"])  # unverified too
        # the dependency is recorded completed -> met, and the version is verified -> fully ready
        self.c.execute("INSERT INTO linear_snapshot VALUES ('fin-2','FIN-2',?,?,'completed',1,?)",
                       ("2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z", _raw(ident="FIN-2", state="completed")))
        self.verify(b["id"], "FIN-1", "fin-1")
        (row,) = strategy.ready(self.cfg, self.c)
        self.assertTrue(row["ready"])
        self.assertEqual(row["blockers"], [])
        # source changed since capture -> needs-amendment, no longer intent-ready
        self.c.execute("INSERT INTO linear_snapshot VALUES ('fin-1','FIN-1',?,?,'unstarted',1,?)",
                       ("2026-09-03T00:00:00Z", "2026-09-03T00:00:00Z", _raw(ident="FIN-1")))
        (row,) = strategy.ready(self.cfg, self.c)
        self.assertFalse(row["intent_ready"])
        self.assertIn("source changed since capture: FIN-1", row["blockers"])

    def test_ready_excludes_consumed_brief(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at,brief_id) "
                       "VALUES ('d1','draft','[]','x',?,?)", (SNAP, b["id"]))
        self.assertEqual(strategy.ready(self.cfg, self.c), [])

    def test_dependency_cycle_rejected(self):
        a = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body(dependencies=["FIN-2"]))
        strategy.approve(self.cfg, self.c, a["id"], "user:dashboard")  # published, so it is in the effective graph
        with self.assertRaises(StageError):  # B depends on A while A (published) depends on B
            strategy.create(self.cfg, self.c, ["FIN-2"], "user:cli", _body(dependencies=["FIN-1"]))

    def test_dependency_cycle_uses_current_published(self):
        # A v1 depends on B; B depends on A -> real current cycle
        a = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body(dependencies=["FIN-2"]))
        strategy.approve(self.cfg, self.c, a["id"], "user:dashboard")
        with self.assertRaises(StageError):
            strategy.create(self.cfg, self.c, ["FIN-2"], "user:cli", _body(dependencies=["FIN-1"]))
        # publish A v2 removing the dependency -> the historical A v1 no longer blocks
        a2 = strategy.revise(self.cfg, self.c, a["id"], _body(dependencies=[]), "remove dep", "user:cli")
        strategy.approve(self.cfg, self.c, a2["id"], "user:dashboard")
        b = strategy.create(self.cfg, self.c, ["FIN-2"], "user:cli", _body(dependencies=["FIN-1"]))
        self.assertEqual(b["state"], "draft")  # allowed: A v2 removed the historical dependency

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

    def test_approve_refuses_source_drift(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        self.c.execute("INSERT INTO linear_snapshot VALUES ('fin-1','FIN-1',?,?,'unstarted',1,?)",
                       ("2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z", _raw(ident="FIN-1")))
        with self.assertRaises(StageError):
            strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")

    def test_agent_cannot_publish(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "agent:factory-chat", _body())
        with self.assertRaises(StageError):
            strategy.approve(self.cfg, self.c, b["id"], "agent:factory-chat")
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")  # a person may
        with self.assertRaises(StageError):
            strategy.hold(self.cfg, self.c, b["id"], "why", "factory:propose")

    def test_ready_requires_exact_version_verdict(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        (row,) = strategy.ready(self.cfg, self.c)
        self.assertFalse(row["verified"])  # the captured source verdict is never borrowed
        self.assertFalse(row["ready"])
        self.verify(b["id"], "FIN-1", "fin-1")
        (row,) = strategy.ready(self.cfg, self.c)
        self.assertTrue(row["verified"])
        self.assertTrue(row["ready"])

    def test_draft_amendment_does_not_supersede_approved(self):
        a = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        strategy.approve(self.cfg, self.c, a["id"], "user:dashboard")
        self.verify(a["id"], "FIN-1", "fin-1")
        strategy.revise(self.cfg, self.c, a["id"], _body(title="T2"), "source changed", "user:cli")
        (row,) = strategy.ready(self.cfg, self.c)  # the approved intent is still current
        self.assertEqual(row["id"], a["id"])
        self.assertTrue(row["ready"])

    def test_superseding_approval_queues_only_latest(self):
        a = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        strategy.approve(self.cfg, self.c, a["id"], "user:dashboard")
        self.verify(a["id"], "FIN-1", "fin-1")
        draft = strategy.revise(self.cfg, self.c, a["id"], _body(title="T2"), "amended", "user:cli")
        strategy.approve(self.cfg, self.c, draft["id"], "user:dashboard")
        self.verify(draft["id"], "FIN-1", "fin-1")
        self.assertEqual([r["id"] for r in strategy.ready(self.cfg, self.c)], [draft["id"]])
        self.assertEqual(strategy.get(self.c, a["id"])["state"], "approved")  # prior version stays readable

    def test_brief_verdict_issue_consistency(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        other = self.c.execute("SELECT id FROM verdict WHERE issue_id='fin-2'").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("INSERT INTO brief_verdict(brief_id,issue_id,verdict_id) VALUES (?,?,?)",
                           (b["id"], "fin-1", other))

    def test_revise_always_appends_new_version(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body(title="T"))
        b2 = strategy.revise(self.cfg, self.c, b["id"], _body(title="T2"), "edit", "user:cli")
        self.assertNotEqual(b2["id"], b["id"])
        self.assertEqual(b2["revision"], 2)
        self.assertEqual(b2["parent_id"], b["id"])
        self.assertEqual(strategy.get(self.c, b["id"])["body"]["title"], "T")  # draft never mutated in place
        self.assertEqual(b2["body"]["title"], "T2")

    def test_linear_lineage_blocks_branching(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        strategy.revise(self.cfg, self.c, b["id"], _body(title="T2"), "e", "user:cli")
        with self.assertRaises(StageError):  # same parent cannot have a second child
            strategy.revise(self.cfg, self.c, b["id"], _body(title="T3"), "e2", "user:cli")

    def test_explicit_global_resource_is_preserved(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli",
                            _body(resources=["global:*", "clickhouse:serving/ck_dev"]))
        self.assertIn("global:*", b["body"]["resources"])
        self.assertIn("clickhouse:serving/ck_dev", b["body"]["resources"])
        # a human can drop global for repo-only work
        b2 = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli",
                             _body(resources=["clickhouse:serving/ck_dev"]))
        self.assertNotIn("global:*", b2["body"]["resources"])

    def test_agent_cannot_signal_resource_review(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "agent:factory-chat",
                            _body(resources=["clickhouse:serving/ck_dev"]))
        self.assertEqual(b["body"]["resources"],
                         ["repo:Finks-ai/finks-ddd", "route:fx-news", "global:*"])

    def test_groom_prompt_keeps_complete_sources_and_rejects_oversize(self):
        src = {"identifier": "FIN-1", "title": "t", "url": "u", "repo": "r", "context": "c",
               "description": "x" * 5000, "verdict_kind": "valid", "verdict_reason": "y" * 5000,
               "evidence": ["e" * 5000]}
        prompt = strategy._groom_prompt([src])
        self.assertIn("x" * 5000, prompt)  # full description preserved
        self.assertIn("e" * 5000, prompt)  # full evidence preserved
        with self.assertRaises(StageError):  # oversize -> clear fewer-sources error, never silent truncation
            strategy._groom_prompt([dict(src, description="z" * 300_000)])

    def test_agent_can_create_amendment_draft(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body(title="T"))
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        draft = strategy.revise(self.cfg, self.c, b["id"], _body(title="T2"), "agent amended", "agent:factory-chat")
        self.assertEqual(draft["state"], "draft")  # unapproved amendment draft is allowed
        self.assertEqual(draft["parent_id"], b["id"])
        self.assertEqual(strategy.get(self.c, b["id"])["state"], "approved")  # prior intent immutable
        with self.assertRaises(StageError):  # approval is still human-only
            strategy.approve(self.cfg, self.c, draft["id"], "agent:factory-chat")

    def test_dependency_reopened_verdict_not_ready(self):
        # an already-done verdict captured at the old snapshot is not readiness once the ticket reopened
        self.c.execute("UPDATE verdict SET superseded_at=? WHERE issue_id='fin-2' AND superseded_at IS NULL", (SNAP,))
        self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,context,repo,kind,reason,evidence_json,"
                       "created_at,created_by) VALUES ('fin-2',?,'ctx','Finks-ai/finks-ddd','already-done','r',"
                       "'[\"e\"]',?,'prune')", (SNAP, SNAP))
        self.assertEqual(strategy._dependency_status(self.c, "FIN-2"), "ready")  # verdict matches current snapshot
        self.c.execute("INSERT INTO linear_snapshot VALUES ('fin-2','FIN-2',?,?,'unstarted',1,?)",
                       ("2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z", _raw(ident="FIN-2")))
        self.assertEqual(strategy._dependency_status(self.c, "FIN-2"), "unmet")  # old verdict no longer matches
        self.c.execute("INSERT INTO linear_snapshot VALUES ('fin-2','FIN-2',?,?,'completed',1,?)",
                       ("2026-09-03T00:00:00Z", "2026-09-03T00:00:00Z", _raw(ident="FIN-2", state="completed")))
        self.assertEqual(strategy._dependency_status(self.c, "FIN-2"), "ready")  # latest completed still completion

    def test_resource_canonical_boundary(self):
        for r in ("clickhouse:serving/", "clickhouse:/serving", "clickhouse:serving//ck", "clickhouse:./x",
                  "clickhouse:x/..", "clickhouse:*", "clickhouse:serving/*", "clickhouse:x/./y",
                  "CLICKHOUSE:serving"):
            with self.subTest(resource=r), self.assertRaises(StageError):
                strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body(resources=[r]))
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli",
                            _body(resources=["global:*", "clickhouse:serving/ck_dev/master_profiles"]))
        self.assertIn("global:*", b["body"]["resources"])
        self.assertIn("clickhouse:serving/ck_dev/master_profiles", b["body"]["resources"])

    def test_dispatch_resource_rejects_unsafe_alias(self):
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
                       "VALUES ('d1','draft','[]','x',?)", (SNAP,))
        self.c.execute("INSERT INTO dispatch_resource VALUES ('d1','global:*')")
        for bad in ("clickhouse:serving/", "clickhouse:*", "CLICKHOUSE:serving", "clickhouse:x/..",
                    "clickhouse:serving//ck"):
            with self.subTest(resource=bad), self.assertRaises(sqlite3.IntegrityError):
                self.c.execute("INSERT INTO dispatch_resource VALUES ('d1',?)", (bad,))

    def test_launch_restart_reservation_and_sent_uncertain(self):
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
                       "VALUES ('d1','draft','[]','x',?)", (SNAP,))
        self.c.execute("INSERT INTO dispatch_resource VALUES ('d1','global:*')")
        self.c.execute("UPDATE dispatch SET state='staged', body_sha256='h', approved_by='u' WHERE run_id='d1'")
        self.c.execute("UPDATE dispatch SET state='executing', executing_at=?, executor_pane='p1' WHERE run_id='d1'",
                       (SNAP,))
        with self.assertRaises(sqlite3.IntegrityError):  # an executing run reserves only its own pane
            self.c.execute("INSERT INTO dispatch_launch(run_id,pane_id,state,claimed_at) VALUES ('d1','p2','reserved',?)",
                           (SNAP,))
        self.c.execute("INSERT INTO dispatch_launch(run_id,pane_id,state,claimed_at) VALUES ('d1','p1','reserved',?)",
                       (SNAP,))
        self.c.execute("UPDATE dispatch_launch SET state='sent', sent_at=? WHERE run_id='d1'", (SNAP,))
        self.c.execute("UPDATE dispatch_launch SET state='uncertain' WHERE run_id='d1'")  # sent -> uncertain restart
        self.c.execute("UPDATE dispatch_launch SET state='sent', sent_at=? WHERE run_id='d1'", (SNAP,))  # success

    def test_backlog_source_visible_and_groomable_but_not_execution_ready(self):
        # an OWNED raw Backlog ticket (in_scope=0), foreign-assigned — must still be a visible source
        self.c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,'backlog',0,?)",
                       ("fin-3", "FIN-3", SNAP, SNAP,
                        _raw(ident="FIN-3", state="Backlog", assignee={"email": "niko@example.com"})))
        tickets = strategy.overview(self.cfg, self.c)["tickets"]
        fin3 = next(t for t in tickets if t["identifier"] == "FIN-3")
        self.assertEqual(fin3["state_type"], "backlog")
        self.assertEqual(fin3["reason"], "assigned to someone else")  # truthful blocker, not silently hidden
        # a foreign-domain (not owned) ticket is never a source (scope safeguard)
        self.c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,'backlog',0,?)",
                       ("fin-9", "FIN-9", SNAP, SNAP,
                        json.dumps({"title": "t9", "url": "https://linear/FIN-9", "identifier": "FIN-9",
                                    "description": "Domain: Other Domain\n", "state": {"name": "Backlog"},
                                    "assignee": None, "priority": 3, "labels": {"nodes": []},
                                    "team": {"key": "TEAM"}})))
        self.assertNotIn("FIN-9", {t["identifier"] for t in strategy.overview(self.cfg, self.c)["tickets"]})
        # the Backlog source is groomable into an unapproved draft (approve is still human-only + validation)...
        b = strategy.create(self.cfg, self.c, ["FIN-3"], "user:cli", _body())
        self.assertEqual(b["state"], "draft")
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        # ...but is NOT execution-eligible: the foreign-assignee safeguard still blocks readiness
        (row,) = strategy.ready(self.cfg, self.c)
        self.assertFalse(row["ready"])
        self.assertTrue(any("assigned to" in blk for blk in row["blockers"]))

    def test_source_metadata_serialization_and_missing_dates(self):
        # a source with a real createdAt + due sidecar round-trips its metadata through sources_json
        raw3 = json.loads(_raw(ident="FIN-3"))
        raw3["createdAt"] = "2026-08-01T00:00:00Z"
        self.c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,'unstarted',1,?)",
                       ("fin-3", "FIN-3", SNAP, SNAP, json.dumps(raw3)))
        self.c.execute("INSERT INTO linear_due(issue_id, snapshot_updated_at, due_date) VALUES (?,?,?)",
                       ("fin-3", SNAP, "2026-10-15"))
        src = strategy.create(self.cfg, self.c, ["FIN-3"], "user:cli", _body())["sources"][0]
        self.assertEqual(src["created_at"], "2026-08-01T00:00:00Z")
        self.assertEqual(src["updated_at"], SNAP)          # the snapshot row's updated_at, never fetched_at
        self.assertEqual(src["due_date"], "2026-10-15")    # the exact ingested version's due, not inferred
        self.assertIsNone(src["verdict_at"])               # no verdict for this source yet
        # missing dates stay null: FIN-1 has no createdAt, no due sidecar, but a current verdict
        s1 = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())["sources"][0]
        self.assertIsNone(s1["created_at"])
        self.assertIsNone(s1["due_date"])
        self.assertEqual(s1["updated_at"], SNAP)
        self.assertEqual(s1["verdict_at"], SNAP)           # the current verdict's created_at

    def test_unmapped_changed_source_verdict_freshness(self):
        # an owned ticket with no mapped context still reports _stale_db freshness for its existing verdict
        self.c.execute("INSERT INTO linear_project VALUES ('p2','other-domain','Other Domain','me@example.com',?)",
                       (SNAP,))
        raw = json.loads(_raw(ident="FIN-4"))
        raw["description"] = "Domain: Other Domain\n"
        # the verdict's pinned snapshot version must exist (FK), then the ticket changes to a newer version
        self.c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,'unstarted',1,?)",
                       ("fin-4", "FIN-4", SNAP, SNAP, json.dumps(raw)))
        self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,context,repo,kind,reason,evidence_json,"
                       "created_at,created_by) VALUES ('fin-4',?,NULL,'Finks-ai/finks-ddd','valid','r','[\"e\"]',?,"
                       "'t')", (SNAP, SNAP))
        self.c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,'unstarted',1,?)",
                       ("fin-4", "FIN-4", "2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z", json.dumps(raw)))
        fin4 = next(t for t in strategy.overview(self.cfg, self.c)["tickets"] if t["identifier"] == "FIN-4")
        self.assertIsNone(fin4["context"])                  # unmapped: no context for "Other Domain"
        self.assertEqual(fin4["verdict"], "valid")
        self.assertEqual(fin4["verdict_at"], SNAP)
        self.assertEqual(fin4["stale"], "ticket-changed")   # freshness computed even though unmapped
        self.assertEqual(fin4["reason"], "unmapped")        # execution blocker stays the context mapping

    def test_repo_only_opt_out_and_explicit_global(self):
        # a human can opt out of global:* with a repo/route-only list (no invented extra key)
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli",
                            _body(resources=["repo:Finks-ai/finks-ddd", "route:fx-news"]))
        self.assertEqual(b["body"]["resources"], ["repo:Finks-ai/finks-ddd", "route:fx-news"])
        strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")  # re-merge preserves the reviewed opt-out
        self.assertEqual(strategy.get(self.c, b["id"])["body"]["resources"],
                         ["repo:Finks-ai/finks-ddd", "route:fx-news"])
        b2 = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body(resources=["global:*"]))
        self.assertIn("global:*", b2["body"]["resources"])  # explicit global:* stays

    def test_approve_rejects_draft_with_child(self):
        b = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        b2 = strategy.revise(self.cfg, self.c, b["id"], _body(title="T2"), "edit", "user:cli")
        with self.assertRaises(StageError):  # b is superseded by b2; approve only the latest
            strategy.approve(self.cfg, self.c, b["id"], "user:dashboard")
        strategy.approve(self.cfg, self.c, b2["id"], "user:dashboard")
        self.assertEqual(strategy.get(self.c, b2["id"])["state"], "approved")

    def test_overview_marks_superseded_and_carries_phase(self):
        a = strategy.create(self.cfg, self.c, ["FIN-1"], "user:cli", _body())
        strategy.approve(self.cfg, self.c, a["id"], "user:dashboard")
        a2 = strategy.revise(self.cfg, self.c, a["id"], _body(title="T2"), "amended", "user:cli")
        strategy.approve(self.cfg, self.c, a2["id"], "user:dashboard")
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at,brief_id) "
                       "VALUES ('d1','draft','[]','x',?,?)", (SNAP, a2["id"]))
        briefs = {b["id"]: b for b in strategy.overview(self.cfg, self.c)["briefs"]}
        self.assertEqual(briefs[a["id"]]["readiness"], "superseded")   # historical published
        self.assertEqual(briefs[a2["id"]]["readiness"], "dispatched")  # current but consumed
        self.assertEqual(briefs[a2["id"]]["dispatch"]["phase"], "draft")

    def test_reserve_guard_admits_only_staged(self):
        self.c.execute("DROP TRIGGER dispatch_execute_guard")  # simulate pre-guard migration: both runs get global:*
        # r2: legacy draft -> done holding global:*
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) VALUES ('r2','draft','[]','x',?)",
                       (SNAP,))
        self.c.execute("INSERT INTO dispatch_resource VALUES ('r2','global:*')")
        self.c.execute("UPDATE dispatch SET state='staged', body_sha256='h', approved_by='u' WHERE run_id='r2'")
        self.c.execute("UPDATE dispatch SET state='executing', executing_at=?, executor_pane='p2' WHERE run_id='r2'", (SNAP,))
        self.c.execute("UPDATE dispatch SET state='done', done_at=? WHERE run_id='r2'", (SNAP,))
        # r1: legacy draft -> executing holding global:*
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) VALUES ('r1','draft','[]','x',?)",
                       (SNAP,))
        self.c.execute("INSERT INTO dispatch_resource VALUES ('r1','global:*')")
        self.c.execute("UPDATE dispatch SET state='staged', body_sha256='h', approved_by='u' WHERE run_id='r1'")
        self.c.execute("UPDATE dispatch SET state='executing', executing_at=?, executor_pane='p1' WHERE run_id='r1'", (SNAP,))
        # a NEW staged run is still refused against r2's held global claim
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) VALUES ('r3','draft','[]','x',?)",
                       (SNAP,))
        self.c.execute("INSERT INTO dispatch_resource VALUES ('r3','global:*')")
        self.c.execute("UPDATE dispatch SET state='staged', body_sha256='h', approved_by='u' WHERE run_id='r3'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("INSERT INTO dispatch_launch(run_id,pane_id,state,claimed_at) VALUES ('r3','p3','reserved',?)",
                           (SNAP,))
        # the executing legacy run's resume bootstrap is NOT re-admitted against r2's claim
        self.c.execute("INSERT INTO dispatch_launch(run_id,pane_id,state,claimed_at) VALUES ('r1','p1','reserved',?)",
                       (SNAP,))


if __name__ == "__main__":
    unittest.main()
