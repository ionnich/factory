"""Reconcile conflict rules: who may change Linear state, and what the agent may change. Run: .venv/bin/python -m unittest"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import db, decide
from factory.dispatch import StageError
from factory.reconcile import _COMPLETION, plan, resolve, state_gate

LEAD = "lead@finks.ai"
CFG = SimpleNamespace(linear={"lead": LEAD})
T0, T1 = "2026-09-01T00:00:00.000Z", "2026-09-02T00:00:00.000Z"


def issue(assignee=None, state_type="unstarted", updated=T0):
    return {"assignee": {"email": assignee} if assignee else None, "updatedAt": updated,
            "state": {"type": state_type, "name": state_type.title()}}


class StateGate(unittest.TestCase):
    def test_unassigned_or_lead_and_fresh_may_write(self):
        self.assertIsNone(state_gate(CFG, issue(), T0))
        self.assertIsNone(state_gate(CFG, issue(LEAD), T0))

    def test_someone_elses_ticket_is_never_moved(self):
        self.assertIn("assigned to", state_gate(CFG, issue("other@finks.ai"), T0))

    def test_ticket_changed_since_verdict_is_never_moved(self):
        self.assertIn("changed since", state_gate(CFG, issue(updated=T1), T0))

    def test_closed_ticket_is_never_reopened_or_moved(self):
        for st in ("completed", "canceled"):
            self.assertIn("already", state_gate(CFG, issue(state_type=st), None))

    def test_dispatch_cards_skip_freshness(self):
        # GitHub integration bumps updatedAt when the PR links; card write-back must not depend on it.
        self.assertIsNone(state_gate(CFG, issue(updated=T1), None))


class Resolve(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        self.c.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1',?,?,'unstarted',1,'{}')", (T0, T0))
        rows = [("comment", {"body": "Duplicate of FIN-9: see https://x.y/z"}, "apply"),
                ("description", {"completion": "## Completion\nPR: https://github.com/o/r/pull/1\n"
                                               "Commit: " + "a" * 40 + "\n"}, "apply"),
                ("state", {"state": "Canceled"}, "apply")]
        for op, payload, decision in rows:
            self.c.execute("INSERT INTO writeback(run_id, issue_id, op, payload_json, decision, rule, status) "
                           "VALUES ('r','i1',?,?,?,'dup','planned')", (op, json.dumps(payload), decision))

    def test_prose_must_keep_every_reference(self):
        with self.assertRaises(StageError):
            resolve(self.c, "r", "FIN-1", "comment", "Duplicate, closing.", None)
        with self.assertRaises(StageError):
            resolve(self.c, "r", "FIN-1", "description", "## Completion\nPR: https://github.com/o/r/pull/1\n", None)
        resolve(self.c, "r", "FIN-1", "comment", "Same work as FIN-9 (https://x.y/z).", None)

    def test_agent_cannot_edit_state_payload(self):
        with self.assertRaises(StageError):
            resolve(self.c, "r", "FIN-1", "state", "Done", None)

    def test_agent_may_only_downgrade(self):
        resolve(self.c, "r", "FIN-1", "state", None, "owner objected in comments")
        self.assertEqual(self.c.execute("SELECT decision FROM writeback WHERE op='state'").fetchone()[0], "flag")
        with self.assertRaises(StageError):
            resolve(self.c, "r", "FIN-1", "state", None, "again")
        with self.assertRaises(sqlite3.DatabaseError):  # raw SQL cannot upgrade either
            self.c.execute("UPDATE writeback SET decision='apply' WHERE op='state'")

    def test_person_may_apply_a_held_write_and_the_agent_cannot_hold_it_again(self):
        resolve(self.c, "r", "FIN-1", "state", None, "reconcile agent: owner objected")
        did = decide.writeback(self.c, "r", "i1", "state", {"state": "Canceled"}, "reconcile agent: owner objected")
        self.assertEqual(decide.one(self.c, did)["recommended"], "skip")
        decide.choose(None, self.c, did, "apply", "user")
        w = self.c.execute("SELECT decision, status, approved_by FROM writeback WHERE op='state'").fetchone()
        self.assertEqual(tuple(w), ("apply", "planned", "user"))
        held = ("r", "i1", "state", json.dumps({"state": "Canceled"}), "flag", "dup", "reconcile agent: again", "planned")
        with mock.patch("factory.reconcile._dispatch_rows", return_value=[held]):
            plan(None, self.c, "r")  # the reconcile gate re-plans a run that still has unsent rows
        w = self.c.execute("SELECT decision, status, approved_by FROM writeback WHERE op='state'").fetchone()
        self.assertEqual(tuple(w), ("apply", "planned", "user"))
        with self.assertRaises(StageError):
            resolve(self.c, "r", "FIN-1", "state", None, "still doubtful")
        with self.assertRaises(StageError):  # answered once
            decide.choose(None, self.c, did, "skip", "user")

    def test_a_code_gate_hold_offers_no_apply(self):
        did = decide.writeback(self.c, "r", "i1", "state", {"state": "Canceled"}, "assigned to someone@else")
        self.assertEqual([o["id"] for o in decide.one(self.c, did)["options"]], ["skip", "manual"])


class CompletionBlock(unittest.TestCase):
    def test_replaces_only_the_completion_section(self):
        desc = "Top\n## Completion\nOutcome: old\n\n## Notes\nkeep\n"
        new = _COMPLETION.sub(lambda _: "## Completion\nOutcome: new\n\n", desc, count=1)
        self.assertEqual(new, "Top\n## Completion\nOutcome: new\n\n## Notes\nkeep\n")


class OwnWrites(unittest.TestCase):
    def test_factory_comment_does_not_retrigger_verification(self):
        from factory.prune import staleness
        c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        for t in (T0, T1):
            c.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1',?,?,'unstarted',1,'{}')", (t, t))
        c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,kind,reason,evidence_json,created_at,created_by) "
                  "VALUES ('i1',?,'already-done','r','[1]',?,'t')", (T0, T0))
        latest = c.execute("SELECT * FROM linear_latest WHERE issue_id='i1'").fetchone()
        self.assertEqual(staleness(None, c, latest, None), "ticket-changed")
        c.execute("INSERT INTO linear_own_write VALUES ('i1', ?)", (T1,))
        self.assertIsNone(staleness(None, c, latest, None))


if __name__ == "__main__":
    unittest.main()
