"""propose never hands off a dispatch a person staged, and retries its own without restaging."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import db, dispatch

SNAP = "2026-09-01T00:00:00Z"


class Propose(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) VALUES ('d1','draft','[]','x',?)",
                       (SNAP,))
        self.cfg = SimpleNamespace(repos={}, raw={})

    def staged_by(self, actor):
        self.c.execute("UPDATE dispatch SET state='staged', body_sha256='h', last_actor=? WHERE run_id='d1'", (actor,))

    def test_person_staged_dispatch_is_left_for_the_person(self):
        self.staged_by("user")
        with mock.patch.object(dispatch, "handoff") as h, mock.patch.object(dispatch, "stage") as s:
            self.assertEqual(dispatch.propose(self.cfg, self.c)["action"], "wait")
        h.assert_not_called()
        s.assert_not_called()

    def test_own_staged_dispatch_is_retried_not_restaged(self):
        self.staged_by(dispatch.PROPOSE)
        with mock.patch.object(dispatch, "handoff", side_effect=dispatch.StageError("busy")) as h, \
                mock.patch.object(dispatch, "stage") as s:
            self.assertEqual(dispatch.propose(self.cfg, self.c)["action"], "staged")
            self.assertEqual(dispatch.propose(self.cfg, self.c)["run_id"], "d1")
        self.assertEqual(h.call_count, 2)
        s.assert_not_called()


if __name__ == "__main__":
    unittest.main()
