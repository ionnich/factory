"""Blocked-brief investigation durability, validation, and amendment races."""
import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from factory import brief_investigate, db, strategy
from factory.config import Config, Context

SNAP = "2026-09-01T00:00:00.000Z"


def raw(identifier, state="Todo", state_type="unstarted"):
    return state_type, json.dumps({"identifier": identifier, "title": f"Work {identifier}",
                                   "url": f"https://linear/{identifier}",
                                   "description": "Domain: My Domain\n", "state": {"name": state},
                                   "team": {"key": "TEAM"}, "assignee": None, "priority": 3,
                                   "labels": {"nodes": []}, "project": {"name": "My Domain"}})


def body(title="Original", dependencies=None):
    return {"title": title, "outcome": "Ship the remaining work", "acceptance": ["Behavior is observable"],
            "scope": ["Implement the source"], "exclusions": [], "decisions": [],
            "dependencies": dependencies or [], "resources": [], "risks": [], "evidence": []}


def output(identifiers, proposed_body=None, summary="Investigation complete"):
    return json.dumps({"identifiers": identifiers, "body": proposed_body, "summary": summary,
                       "evidence": ["Recorded snapshot provenance reviewed"],
                       "followups": [{"title": "Verify production", "description": "Confirm cached state."}]})


class Investigations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.conn = db.connect(root / "factory.db")
        self.addCleanup(self.conn.close)
        self.cfg = Config(raw={"linear": {"lead": "lead@example.com",
                                          "team": {"TEAM": {"review_state": "QA"}}}},
                          db=root / "factory.db", mirrors=root / "mirrors", dispatches=root / "dispatches",
                          contexts=[Context(name="ctx", repo="Org/repo", domains=["My Domain"], route="fx")],
                          repos={}, witnesses={})
        self.conn.execute("INSERT INTO linear_project VALUES ('p','my-domain','My Domain','lead@example.com',?)",
                          (SNAP,))
        for identifier in ("FIN-1", "FIN-2"):
            state_type, payload = raw(identifier)
            self.conn.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,?,1,?)",
                              (identifier.lower(), identifier, SNAP, SNAP, state_type, payload))
            self.conn.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,context,repo,kind,reason,"
                              "evidence_json,created_at,created_by) VALUES (?,?,'ctx','Org/repo','valid','recorded',"
                              "'[\"recorded\"]',?,'test')", (identifier.lower(), SNAP, SNAP))
        self.conn.execute("INSERT INTO repo_trunk VALUES ('Org/repo','main','abc123',?)", (SNAP,))
        draft = strategy.create(self.cfg, self.conn, ["FIN-1"], "user:test", body())
        self.brief = strategy.approve(self.cfg, self.conn, draft["id"], "user:test")
        self.spawned = []

    def request(self):
        return brief_investigate.request(
            self.cfg, self.conn, self.brief["id"],
            spawn=lambda argv, **kwargs: self.spawned.append((argv, kwargs)))

    @staticmethod
    def runner(payload, mutate=None):
        def run(argv, **kwargs):
            if mutate:
                mutate()
            return subprocess.CompletedProcess(argv, 0, payload, "")
        return run

    def test_active_request_deduplicates_and_spawns_detached(self):
        first = self.request()
        second = self.request()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.spawned), 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO brief_investigation(brief_id,status,requested_at,context_json) "
                              "VALUES (?,'pending',?,'{}')", (self.brief["id"], SNAP))

    def test_no_work_completes_with_followups_and_no_fake_draft(self):
        job = self.request()
        result = brief_investigate.run(self.cfg, self.conn, job["id"],
                                       runner=self.runner(output([], None, "Only human QA remains.")))
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["proposal_brief_id"])
        self.assertIsNone(self.conn.execute("SELECT id FROM work_brief WHERE parent_id=?",
                                            (self.brief["id"],)).fetchone())

    def test_corrected_membership_is_unapproved_and_original_is_immutable(self):
        job = self.request()
        result = brief_investigate.run(self.cfg, self.conn, job["id"],
                                       runner=self.runner(output(["FIN-2"], body("Corrected"))))
        proposal = strategy.get(self.conn, result["proposal_brief_id"])
        original = strategy.get(self.conn, self.brief["id"])
        self.assertEqual(proposal["state"], "draft")
        self.assertEqual(proposal["parent_id"], original["id"])
        self.assertEqual([s["identifier"] for s in proposal["sources"]], ["FIN-2"])
        self.assertEqual(original, self.brief)
        self.assertIn("global:*", proposal["body"]["resources"])
        self.assertEqual(self.request()["id"], job["id"])

    def test_unoffered_and_unproven_dependency_outputs_fail_without_draft(self):
        job = self.request()
        failed = brief_investigate.run(self.cfg, self.conn, job["id"],
                                       runner=self.runner(output(["FIN-999"], body())))
        self.assertEqual(failed["status"], "failed")
        retry = self.request()
        failed = brief_investigate.run(self.cfg, self.conn, retry["id"],
                                       runner=self.runner(output(["FIN-2"], body(dependencies=["FIN-999"]))))
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(self.conn.execute("SELECT id FROM work_brief WHERE parent_id=?",
                                            (self.brief["id"],)).fetchone())

    def test_source_change_or_human_amendment_wins_race(self):
        job = self.request()

        def complete_candidate():
            state_type, payload = raw("FIN-2", state="Done", state_type="completed")
            self.conn.execute("INSERT INTO linear_snapshot VALUES ('fin-2','FIN-2',?,?,?,1,?)",
                              ("2026-09-02T00:00:00.000Z", SNAP, state_type, payload))

        failed = brief_investigate.run(self.cfg, self.conn, job["id"],
                                       runner=self.runner(output(["FIN-2"], body("Corrected")), complete_candidate))
        self.assertEqual(failed["status"], "failed")

        # Restore no mutation need: FIN-1 remains eligible and a human child must prevent the retry's late worker.
        retry = self.request()

        def amend():
            strategy.revise(self.cfg, self.conn, self.brief["id"], body("Human amendment"),
                            "human correction", "user:test")

        failed = brief_investigate.run(self.cfg, self.conn, retry["id"],
                                       runner=self.runner(output(["FIN-1"], body("Agent amendment")), amend))
        self.assertEqual(failed["status"], "failed")
        children = self.conn.execute("SELECT created_by FROM work_brief WHERE parent_id=?",
                                     (self.brief["id"],)).fetchall()
        self.assertEqual([row["created_by"] for row in children], ["user:test"])

    def test_failures_are_durable_retryable_and_stale_get_is_pure(self):
        failed = brief_investigate.request(self.cfg, self.conn, self.brief["id"],
                                           spawn=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fork")))
        self.assertEqual(failed["status"], "failed")
        job = self.request()
        malformed = brief_investigate.run(self.cfg, self.conn, job["id"],
                                           runner=self.runner("not json"))
        self.assertEqual(malformed["status"], "failed")
        retry = self.request()
        old = (datetime.now(UTC) - brief_investigate.STALE_AFTER - timedelta(seconds=1)).isoformat()
        self.conn.execute("UPDATE brief_investigation SET requested_at=? WHERE id=?", (old, retry["id"]))
        projected = brief_investigate.rows(self.conn)[self.brief["id"]]
        self.assertEqual(projected["status"], "failed")
        self.assertEqual(self.conn.execute("SELECT status FROM brief_investigation WHERE id=?",
                                           (retry["id"],)).fetchone()["status"], "pending")
        replacement = self.request()
        self.assertNotEqual(replacement["id"], retry["id"])
        retired = self.conn.execute("SELECT status FROM brief_investigation WHERE id=?",
                                    (retry["id"],)).fetchone()["status"]
        self.assertEqual(retired, "failed")



if __name__ == "__main__":
    unittest.main()
