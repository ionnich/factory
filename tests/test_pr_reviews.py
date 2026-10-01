import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factory import db
from factory.pr_reviews import list_reviews
from tests._helpers import seed_dispatch, seed_snapshot, seed_ticket, seed_verdict


class Runner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(args)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def result(stdout="", stderr="", code=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)


def reviewer_user(login="nich"):
    return {"requestedReviewer": {"login": login}}


def reviewer_team(org="acme", slug="platform", name="Platform"):
    return {"requestedReviewer": {"slug": slug, "name": name, "organization": {"login": org}}}


def pr(url, *, author="other", reviewers=(), draft=False, checks="SUCCESS", updated="2026-09-02T00:00:00Z"):
    rollup = None if checks is None else {"state": checks}
    return {
        "url": url, "title": "Review this", "number": int(url.rsplit("/", 1)[1]), "state": "OPEN", "isDraft": draft,
        "createdAt": "2026-09-01T00:00:00Z", "updatedAt": updated,
        "repository": {"nameWithOwner": "acme/repo"}, "author": {"login": author},
        "reviewRequests": {"nodes": list(reviewers)},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]},
    }


def search(nodes, more=False, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}


class PRReviews(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = db.connect(Path(directory.name) / "factory.db")
        self.addCleanup(self.conn.close)

    def associate(self, url):
        seed_snapshot(self.conn, "i1", "FIN-1")
        verdict = seed_verdict(self.conn, "i1")
        seed_dispatch(self.conn, "run-1")
        seed_ticket(self.conn, "run-1", "i1", "FIN-1", verdict)
        self.conn.execute("UPDATE dispatch_ticket SET pr_url=? WHERE run_id='run-1'", (url,))

    def test_normalizes_direct_and_team_requests_and_exact_dispatch(self):
        direct_url = "https://github.com/acme/repo/pull/1"
        team_url = "https://github.com/acme/repo/pull/2"
        self.associate(direct_url)
        teams = json.dumps({"slug": "platform", "name": "Platform", "organization": "acme"}) + "\n"
        payload = {"data": {
            "viewer": {"login": "nich"},
            "direct": search([
                pr(direct_url, reviewers=[reviewer_user()]),
                pr("https://github.com/acme/repo/pull/3", author="nich", reviewers=[reviewer_user()]),
                pr("https://github.com/acme/repo/pull/4", reviewers=[reviewer_user()], draft=True),
            ]),
            "team0": search([pr(team_url, reviewers=[reviewer_team()], checks="PENDING",
                                       updated="2026-09-03T00:00:00Z")]),
        }}
        inbox = list_reviews(self.conn, Runner(result(teams), result(json.dumps(payload))))

        self.assertEqual(inbox["count"], 2)
        self.assertFalse(inbox["partial"])
        self.assertEqual([item["url"] for item in inbox["items"]], [team_url, direct_url])
        self.assertEqual(inbox["items"][0]["request_context"], "Team Platform (acme)")
        self.assertEqual(inbox["items"][0]["checks"], "pending")
        self.assertEqual(inbox["items"][1]["checks"], "passed")
        self.assertEqual(inbox["items"][1]["dispatch"],
                         {"run_id": "run-1", "state": "draft", "phase": "draft"})

    def test_team_access_failure_keeps_direct_results_but_marks_count_unknown(self):
        payload = {"data": {"viewer": {"login": "nich"}, "direct": search([
            pr("https://github.com/acme/repo/pull/5", reviewers=[reviewer_user()], checks=None)
        ])}}
        inbox = list_reviews(self.conn, Runner(result(stderr="missing read:org", code=1),
                                                result(json.dumps(payload))))

        self.assertEqual(len(inbox["items"]), 1)
        self.assertEqual(inbox["items"][0]["checks"], "unknown")
        self.assertIsNone(inbox["count"])
        self.assertTrue(inbox["partial"])
        self.assertIn("missing read:org", inbox["warnings"][0])

    def test_github_failure_is_not_reported_as_empty_inbox(self):
        inbox = list_reviews(self.conn, Runner(FileNotFoundError("gh unavailable"),
                                                FileNotFoundError("gh unavailable")))

        self.assertIsNone(inbox["count"])
        self.assertTrue(inbox["partial"])
        self.assertIn("gh unavailable", inbox["error"])

    def test_stale_search_result_without_current_request_is_excluded(self):
        payload = {"data": {"viewer": {"login": "nich"}, "direct": search([
            pr("https://github.com/acme/repo/pull/6", reviewers=[reviewer_user("someone-else")])
        ])}}
        inbox = list_reviews(self.conn, Runner(result(""), result(json.dumps(payload))))

        self.assertEqual(inbox["items"], [])
        self.assertEqual(inbox["count"], 0)

    def test_paginates_review_searches(self):
        first = pr("https://github.com/acme/repo/pull/7", reviewers=[reviewer_user()])
        second = pr("https://github.com/acme/repo/pull/8", reviewers=[reviewer_user()])
        initial = {"data": {"viewer": {"login": "nich"}, "direct": search([first], True, "page-2")}}
        next_page = {"data": {"direct": search([second])}}
        runner = Runner(result(""), result(json.dumps(initial)), result(json.dumps(next_page)))

        inbox = list_reviews(self.conn, runner)

        self.assertEqual(inbox["count"], 2)
        self.assertFalse(inbox["truncated"])
        self.assertEqual([item["url"] for item in inbox["items"]], [first["url"], second["url"]])

    def test_truncated_results_never_claim_a_complete_count(self):
        first = pr("https://github.com/acme/repo/pull/9", reviewers=[reviewer_user()])
        payload = {"data": {"viewer": {"login": "nich"}, "direct": search([first], True, "more")}}
        with patch("factory.pr_reviews.MAX_ITEMS", 1):
            inbox = list_reviews(self.conn, Runner(result(""), result(json.dumps(payload))))
        self.assertEqual([item["url"] for item in inbox["items"]], [first["url"]])
        self.assertTrue(inbox["truncated"])
        self.assertIsNone(inbox["count"])


if __name__ == "__main__":
    unittest.main()
