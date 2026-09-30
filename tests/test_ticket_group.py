"""Tickets-tab grouping: a live dispatch wins over everything, done beats out-of-scope, a valid ticket someone else
holds is not ready, and an unverified or stale verdict never counts as ready or needing an answer."""
import unittest

from factory.cli import group

VALID = {"kind": "valid", "reason": "ok"}
OURS = {"identifier": "FIN-1", "state_type": "unstarted", "owned": True, "in_scope": True, "context": "api",
        "freshness": "fresh", "verdict": VALID}


class Group(unittest.TestCase):
    def test_precedence(self):
        g = lambda **kw: group({**OURS, **kw}, set())
        self.assertEqual(g(), "ready")
        self.assertEqual(g(dispatch={"state": "executing"}, owned=False), "dispatch")
        self.assertEqual(g(dispatch={"state": "archived"}), "ready")
        self.assertEqual(g(owned=False), "not")
        self.assertEqual(g(state_type="completed", in_scope=False), "done")
        self.assertEqual(g(in_review=True), "done")
        self.assertEqual(g(in_scope=False), "not")
        self.assertEqual(g(context=None), "not")
        self.assertEqual(g(freshness="aged"), "stale")
        self.assertEqual(g(verdict=None), "stale")
        self.assertEqual(g(verdict={"kind": "needs-clarification"}), "answer")
        self.assertEqual(g(verdict={"kind": "invalid-references"}), "answer")
        self.assertEqual(g(verdict={"kind": "duplicate-of"}), "not")
        self.assertEqual(group(OURS, {"FIN-1"}), "not")


if __name__ == "__main__":
    unittest.main()
