"""A timed-out git fetch must not fail every other mirror on the propose tick."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factory import db, repos
from factory.config import Config, Context

SNAP = "2026-09-01T00:00:00Z"


class SyncAll(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.c = db.connect(root / "t.db")
        self.addCleanup(self.c.close)
        self.cfg = Config(
            raw={}, db=root / "t.db", mirrors=root, dispatches=root / "d",
            contexts=[Context("a", "o/a"), Context("b", "o/b")],
            repos={"o/a": {"trunk": "main"}, "o/b": {"trunk": "main"}}, witnesses={})
        self.c.execute("INSERT INTO repo_trunk VALUES ('o/a','main','oldsha',?)", (SNAP,))

    def test_one_timeout_keeps_last_sha_and_fetches_the_other(self):
        def fetch(cfg, repo):
            if repo == "o/a":
                raise subprocess.TimeoutExpired(["git", "fetch"], 300)
            return "newsha-b"

        with mock.patch("factory.repos.fetch", side_effect=fetch), \
                mock.patch("factory.repos.secret", return_value="t"):
            shas, err = repos.sync_all(self.cfg, self.c)
        self.assertEqual(shas["o/a"], "oldsha")
        self.assertEqual(shas["o/b"], "newsha-b")
        self.assertIn("TimeoutExpired", err["o/a"])
        self.assertNotIn("o/b", err)
        self.assertEqual(self.c.execute("SELECT sha FROM repo_trunk WHERE repo='o/b'").fetchone()[0], "newsha-b")
        self.assertEqual(self.c.execute("SELECT sha FROM repo_trunk WHERE repo='o/a'").fetchone()[0], "oldsha")


if __name__ == "__main__":
    unittest.main()
