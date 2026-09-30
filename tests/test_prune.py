import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import db, prune, witness

SNAP = "2026-09-01T00:00:00Z"


class OwnedInScope(unittest.TestCase):
    def test_tickets_in_a_live_dispatch_are_not_rejudged_but_drafts_are(self):
        c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        c.execute("INSERT INTO linear_project VALUES ('p','s','P','lead@x',?)", (SNAP,))
        raw = json.dumps({"state": {"name": "Todo"}, "team": {"key": "FIN"}})
        for run, n in (("d1", 1), ("d2", 2)):
            c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,'unstarted',1,?)", (f"i{n}", f"FIN-{n}", SNAP, SNAP, raw))
            c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) VALUES (?,'draft','[]','x',?)",
                      (run, SNAP))
            c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,repo,kind,reason,evidence_json,created_at,"
                      "created_by) VALUES (?,?,'r1','valid','r','[1]',?,'t')", (f"i{n}", SNAP, SNAP))
            c.execute("INSERT INTO dispatch_ticket(run_id,issue_id,identifier,snapshot_updated_at,verdict_id) "
                      "VALUES (?,?,?,?,?)", (run, f"i{n}", f"FIN-{n}", SNAP, n))
        c.execute("UPDATE dispatch SET state='staged', body_sha256='h', approved_by='u', last_actor='p' WHERE run_id='d1'")
        c.execute("UPDATE dispatch SET state='executing', last_actor='fm-main' WHERE run_id='d1'")
        cfg = SimpleNamespace(linear={"lead": "lead@x"})
        with mock.patch.object(prune, "owned", return_value=True):
            self.assertEqual([s["issue_id"] for s in prune.owned_in_scope(cfg, c)], ["i2"])


class Recheck(unittest.TestCase):
    def test_a_recheck_carries_the_prior_verdict_and_only_the_cited_files_diff(self):
        repo = Path(tempfile.mkdtemp())
        git = lambda *a: prune.repos.git(repo, *a)
        git("init", "-q")
        git("config", "user.email", "t@x"); git("config", "user.name", "t")
        (repo / "cited.py").write_text("x = 1\n"); (repo / "other.py").write_text("y = 1\n")
        git("add", "."); git("commit", "-qm", "one")
        old = git("rev-parse", "HEAD").strip()
        (repo / "cited.py").write_text("x = 2\n"); (repo / "other.py").write_text("y = 2\n")
        git("commit", "-qam", "two")
        new = git("rev-parse", "HEAD").strip()
        c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        c.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1',?,?,'unstarted',1,'{}')", (SNAP, SNAP))
        c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,repo,trunk_sha,kind,reason,evidence_json,"
                  "evidence_paths_json,created_at,created_by) VALUES ('i1',?,'r','" + old + "','valid','x is 1',"
                  "'[{\"type\":\"file\",\"path\":\"cited.py\"}]','[\"cited.py\"]',?,'t')", (SNAP, SNAP))
        cfg = SimpleNamespace(mirror_path=lambda _: repo)
        s, ctx = {"issue_id": "i1"}, SimpleNamespace(repo="r")
        got = prune._recheck(cfg, c, s, ctx, "evidence-changed", {"sha": new})
        self.assertEqual((got["prior"]["kind"], got["prior"]["reason"]), ("valid", "x is 1"))
        self.assertIn("+x = 2", got["cited_diff"])
        self.assertNotIn("other.py", got["cited_diff"])
        self.assertEqual(prune._recheck(cfg, c, s, ctx, "aged", {"sha": old})["cited_diff"],
                         "(no change to the cited files)")
        self.assertEqual(prune._recheck(cfg, c, s, ctx, "ticket-changed", {"sha": new}), {})


class Dagster(unittest.TestCase):
    def run_q(self, query):
        return witness._dagster(SimpleNamespace(), {"url": "http://d", "token_env": "T"}, query)

    def test_http_error_surfaces_graphql_message(self):
        body = json.dumps({"errors": [{"message": 'Cannot query field "foo"'}]}).encode()
        err = urllib.error.HTTPError("http://d", 400, "Bad Request", {}, io.BytesIO(body))
        with mock.patch.object(witness, "secret", return_value="t"), \
                mock.patch.object(witness.urllib.request, "urlopen", side_effect=err), \
                self.assertRaisesRegex(witness.WitnessError, 'HTTP 400: Cannot query field "foo"'):
            self.run_q("{ foo }")

    def test_sql_is_rejected(self):
        with self.assertRaisesRegex(witness.WitnessError, "not SQL"):
            self.run_q("SELECT 1")


if __name__ == "__main__":
    unittest.main()
