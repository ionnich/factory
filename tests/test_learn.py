"""Learnings: a code map harvested from cited evidence expires when trunk changes the path; a pitfall from a block
reaches agents only once the user keeps it; gates get only what bears on the ticket."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from factory import db, decide, learn, repos

SNAP = "2026-09-01T00:00:00Z"


class Learnings(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.git = lambda *a: repos.git(self.repo, *a)
        self.git("init", "-q")
        self.git("config", "user.email", "t@x"); self.git("config", "user.name", "t")
        (self.repo / "cited.py").write_text("x = 1\n"); (self.repo / "other.py").write_text("y = 1\n")
        self.git("add", "."); self.git("commit", "-qm", "one")
        self.sha = self.git("rev-parse", "HEAD")
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        self.cfg = SimpleNamespace(mirror_path=lambda _: self.repo, raw={})
        self.trunk(self.sha)
        ev = [{"type": "file", "path": "cited.py", "note": "x lives here", "sha": self.sha}]
        self.c.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1',?,?,'unstarted',1,'{}')", (SNAP, SNAP))
        self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,repo,trunk_sha,kind,reason,evidence_json,"
                       "evidence_paths_json,created_at,created_by) VALUES ('i1',?,'r',?,'valid','x',?,?,?,'t')",
                       (SNAP, self.sha, json.dumps(ev), '["cited.py"]', SNAP))

    def trunk(self, sha):
        self.c.execute("INSERT OR REPLACE INTO repo_trunk VALUES ('r','main',?,?)", (sha, SNAP))

    def learnings(self):
        return {r["body"]: r["status"] for r in self.c.execute("SELECT body, status FROM learning")}

    def test_codemap_from_file_evidence_expires_when_trunk_changes_its_path(self):
        learn.sync(self.cfg, self.c)
        self.assertEqual(self.learnings(), {"cited.py: x lives here": "active"})
        (self.repo / "other.py").write_text("y = 2\n")
        self.git("commit", "-qam", "elsewhere")
        self.trunk(self.git("rev-parse", "HEAD"))
        learn.sync(self.cfg, self.c)
        self.assertEqual(self.learnings(), {"cited.py: x lives here": "active"})  # untouched: still true
        (self.repo / "cited.py").write_text("x = 2\n")
        self.git("commit", "-qam", "moved x")
        self.trunk(self.git("rev-parse", "HEAD"))
        learn.sync(self.cfg, self.c)
        self.assertEqual(self.learnings(), {"cited.py: x lives here": "expired"})  # and not harvested again

    def block(self):
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) VALUES ('d1','draft',?,'x',?)",
                       (json.dumps([{"repo": "r", "trunk_sha": self.sha}]), SNAP))
        self.c.execute("INSERT INTO dispatch_ticket(run_id,issue_id,identifier,snapshot_updated_at,verdict_id) "
                       "VALUES ('d1','i1','FIN-1',?,1)", (SNAP,))
        decide.blocked(self.c, "d1", "FIN-1", "i1", "the test DB needs the prod role", "lead")
        learn.sync(self.cfg, self.c)
        return next(d for d in decide.rows(self.c) if d["kind"] == "learning")

    def test_a_block_proposes_a_pitfall_that_reaches_agents_once_kept(self):
        d = self.block()
        pitfall = "FIN-1 blocked: the test DB needs the prod role"
        self.assertEqual(self.learnings()[pitfall], "proposed")
        self.assertNotIn(pitfall, "\n".join(learn.relevant(self.c, {"r"})))
        decide.choose(self.cfg, self.c, d["id"], "keep", "user")
        self.assertEqual(self.learnings()[pitfall], "active")
        self.assertEqual(learn.pitfalls(self.c, ["r"]), [f"- L{d['ref']} (r): {pitfall}"])
        learn.sync(self.cfg, self.c)
        self.assertEqual([x["kind"] for x in decide.rows(self.c)], ["blocked"])  # proposed once

    def test_gate_context_holds_rules_for_the_repo_and_map_lines_for_its_paths_only(self):
        self.block()
        self.c.execute("UPDATE learning SET status='active'")
        for scope, path in (("r", "other.py"), ("r", "deep/dir/named.rs"), ("elsewhere", "cited.py")):
            self.c.execute("INSERT INTO learning(kind,scope,body,anchors_json,source,status,created_at) "
                           "VALUES ('codemap',?,?,?,'t','active',?)", (scope, f"{path}: ...", json.dumps([path]), SNAP))
        got = learn.relevant(self.c, {"r"}, ["cited.py"], "fix the parser in named.rs")
        self.assertEqual([line.split(": ", 1)[1] for line in got],
                         ["FIN-1 blocked: the test DB needs the prod role", "deep/dir/named.rs: ...",
                          "cited.py: x lives here"])
        self.assertEqual(len(learn.relevant(self.c, {"r"}, ["cited.py"], "named.rs", cap=1)), 1)

    def test_citing_counts_a_use_once_per_write(self):
        learn.sync(self.cfg, self.c)
        learn.cite(self.c, "L1 said where x lives; L1 again", None, "L99")
        self.assertEqual(self.c.execute("SELECT uses FROM learning WHERE id=1").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
