"""Propose cron: ingest failures stay local, heartbeat is on stderr, Hermex stdout stays empty."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import cli, db


class ProposeTick(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "t.db"
        self.c = db.connect(self.path)
        self.addCleanup(self.c.close)
        self.cfg = SimpleNamespace(db=self.path, raw={}, repos={})

    def _run(self, ingest, announce=True, notify=None):
        notified = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("factory.cli.ingest", ingest))
            for name in ("decide.acknowledge_notifications", "decide.sweep", "jev.refresh",
                         "costs.sync", "learn.sync"):
                stack.enter_context(mock.patch("factory.cli." + name, return_value=[]))
            stack.enter_context(mock.patch("factory.cli.dispatch.propose", return_value={}))
            stack.enter_context(mock.patch(
                "factory.cli.decide.notify",
                side_effect=lambda *a, **k: notified.append(1) or (notify or [])))
            stack.enter_context(mock.patch("factory.cli.brief_propose.propose",
                                           return_value={"status": "idle"}))
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cli.cmd_propose(self.cfg, self.c, SimpleNamespace(announce=announce))
        return out.getvalue(), err.getvalue(), notified

    def test_announce_empty_stdout_writes_stderr_heartbeat_and_file(self):
        out, err, notified = self._run(return_value := mock.Mock(return_value={"fetched": 3, "trunks": {"r": "abc"}}))
        self.assertEqual(out, "")
        self.assertEqual(notified, [1])
        beat = json.loads(err.strip().splitlines()[-1])
        self.assertEqual(beat["factory"], "propose")
        self.assertEqual(beat["fetched"], 3)
        self.assertEqual(beat["messages"], 0)
        self.assertEqual(json.loads((self.root / "factory" / "propose-last.json").read_text())["fetched"], 3)

    def test_ingest_timeout_still_notifies_and_heartbeats_error(self):
        out, err, notified = self._run(mock.Mock(side_effect=TimeoutError("read timed out")))
        self.assertEqual(out, "")
        self.assertEqual(notified, [1])
        beat = json.loads(err.strip().splitlines()[-1])
        self.assertTrue(any("TimeoutError" in e for e in beat["errors"]))

    def test_ingest_isolates_linear_timeout_and_still_records_trunks(self):
        with mock.patch("factory.cli.linear.sync_projects", side_effect=TimeoutError("projects")), \
                mock.patch("factory.cli.linear.ingest", side_effect=TimeoutError("read timed out")), \
                mock.patch("factory.cli.repos.sync_all", return_value=({"o/r": "deadbeefdead"}, {})):
            res = cli.ingest(self.cfg, self.c)
        self.assertEqual(res["trunks"]["o/r"], "deadbeefdead")
        self.assertTrue(any("linear:" in e for e in res["errors"]))
        self.assertTrue(any("projects:" in e for e in res["errors"]))


if __name__ == "__main__":
    unittest.main()
