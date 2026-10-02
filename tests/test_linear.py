"""sync_projects regression: refresh must upsert without destroying domain_review-referenced identities.

The reported failure was a FOREIGN KEY constraint on linear_project.id -> domain_review.domain_id
fired by the old blanket DELETE. These tests exercise the real schema (db.connect, foreign_keys ON)
against a recorded/fake fetch response, with no live writes.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import db, linear

SNAP = "2026-09-01T00:00:00Z"
LEAD = "lead@x"


def cfg():
    return SimpleNamespace(linear={"lead": LEAD})


def proj(pid, slug, name, lead):
    return {"id": pid, "slugId": slug, "name": name, "lead": {"email": lead} if lead else None}


def page(*nodes):
    return {"projects": {"nodes": list(nodes), "pageInfo": {"hasNextPage": False, "endCursor": None}}}


def seed_project(c, pid, slug, name, lead=LEAD):
    c.execute("INSERT INTO linear_project(id, slug_id, name, lead_email, fetched_at) VALUES (?,?,?,?,?)",
              (pid, slug, name, lead, SNAP))


def seed_review(c, domain_id):
    c.execute("INSERT INTO domain_review(domain_id, goal, status, requested_at, completed_at, result_json, "
              "context_json) VALUES (?,'','completed',?,?,?,?)", (domain_id, SNAP, SNAP, "{}", "{}"))


class SyncProjects(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")

    def test_foreign_key_is_real(self):
        # Proves the regression surface: a raw DELETE of a referenced project really is refused.
        seed_project(self.c, "p1", "slug-1", "My Domain")
        seed_review(self.c, "p1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("DELETE FROM linear_project WHERE id='p1'")

    def test_refresh_upserts_referenced_project_and_preserves_review(self):
        seed_project(self.c, "p1", "slug-1", "Old Name")
        seed_review(self.c, "p1")
        with mock.patch("factory.linear.gql",
                        return_value=page(proj("p1", "slug-1", "New Name", LEAD))):
            self.assertEqual(linear.sync_projects(cfg(), self.c), 1)
        row = self.c.execute("SELECT * FROM linear_project WHERE id='p1'").fetchone()
        self.assertEqual((row["name"], row["lead_email"]), ("New Name", LEAD))  # renamed in place, id stable
        self.assertEqual(self.c.execute("SELECT count(*) FROM domain_review WHERE domain_id='p1'").fetchone()[0], 1)

    def test_reassignment_updates_owner_in_place_without_touching_review(self):
        seed_project(self.c, "p1", "slug-1", "My Domain")
        seed_review(self.c, "p1")
        with mock.patch("factory.linear.gql",
                        return_value=page(proj("p1", "slug-1", "My Domain", "other@x"))):
            linear.sync_projects(cfg(), self.c)
        row = self.c.execute("SELECT * FROM linear_project WHERE id='p1'").fetchone()
        self.assertEqual(row["lead_email"], "other@x")
        self.assertEqual(self.c.execute("SELECT count(*) FROM domain_review WHERE domain_id='p1'").fetchone()[0], 1)

    def test_removed_unreferenced_project_is_deleted(self):
        seed_project(self.c, "p1", "slug-1", "Kept")
        seed_project(self.c, "p2", "slug-2", "Gone")
        with mock.patch("factory.linear.gql", return_value=page(proj("p1", "slug-1", "Kept", LEAD))):
            linear.sync_projects(cfg(), self.c)
        self.assertIsNone(self.c.execute("SELECT 1 FROM linear_project WHERE id='p2'").fetchone())

    def test_removed_referenced_project_keeps_identity_but_loses_ownership(self):
        seed_project(self.c, "p1", "slug-1", "Removed")
        seed_project(self.c, "p2", "slug-2", "Kept")
        seed_review(self.c, "p1")  # the review pins the p1 identity
        with mock.patch("factory.linear.gql", return_value=page(proj("p2", "slug-2", "Kept", LEAD))):
            linear.sync_projects(cfg(), self.c)
        # The identity survives the refresh (review still resolvable) ...
        row = self.c.execute("SELECT * FROM linear_project WHERE id='p1'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(self.c.execute("SELECT count(*) FROM domain_review WHERE domain_id='p1'").fetchone()[0], 1)
        # ... but it is no longer owned, so the exact predicate domain_groom.list_ uses excludes it.
        self.assertIsNone(row["lead_email"])
        self.assertIsNone(self.c.execute("SELECT 1 FROM linear_project WHERE id='p1' AND lead_email=?",
                                         (LEAD,)).fetchone())

    def test_failed_fetch_changes_nothing(self):
        seed_project(self.c, "p1", "slug-1", "My Domain")
        seed_review(self.c, "p1")
        before = dict(self.c.execute("SELECT * FROM linear_project WHERE id='p1'").fetchone())
        with mock.patch("factory.linear.gql", side_effect=RuntimeError("linear: boom")):
            with self.assertRaises(RuntimeError):
                linear.sync_projects(cfg(), self.c)
        after = dict(self.c.execute("SELECT * FROM linear_project WHERE id='p1'").fetchone())
        self.assertEqual(after, before)
        self.assertEqual(self.c.execute("SELECT count(*) FROM domain_review WHERE domain_id='p1'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
