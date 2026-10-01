"""Strategy CLI slice: the boundary behaviors the CLI enforces on its own — the brief-body JSON parsing boundary,
and the explicit human confirmations/refusals before a write. Everything wired through to `factory.strategy`,
`factory.scheduler`, `dispatch.stage`/`stage_brief`/`release_unsent` and `prune.put` is covered by the parent's
integration tests, not re-asserted here."""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import cli, dispatch


class JsonBody(unittest.TestCase):
    """`_json_body` parses the exact brief-body forms the CLI accepts and rejects the rest."""

    def test_inline_object(self):
        self.assertEqual(cli._json_body('{"title": "x"}'), {"title": "x"})

    def test_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.json"
            p.write_text('{"title": "x"}')
            self.assertEqual(cli._json_body(str(p)), {"title": "x"})

    def test_from_stdin(self):
        with mock.patch.object(sys, "stdin", io.StringIO('{"title": "x"}')):
            self.assertEqual(cli._json_body("-"), {"title": "x"})

    def test_refuses_a_non_object(self):
        with self.assertRaises(dispatch.StageError):
            cli._json_body("[1, 2]")

    def test_refuses_invalid_json(self):
        with self.assertRaises(dispatch.StageError):
            cli._json_body("{not json")


class StageBoundary(unittest.TestCase):
    def test_brief_refuses_mixed_ticket_identifiers(self):
        # an explicit brief id and ticket identifiers are mutually exclusive, refused before anything is written
        with self.assertRaises(dispatch.StageError):
            cli.cmd_stage("cfg", "conn", SimpleNamespace(brief_id=7, identifiers=["FIN-1"], actor="u"))


class RecoverLaunchBoundary(unittest.TestCase):
    def test_missing_confirm_unsent_is_refused(self):
        # a human must attest the send never landed; without --confirm-unsent the recovery is refused
        with self.assertRaises(dispatch.StageError):
            cli.cmd_recover_launch("cfg", "conn",
                                   SimpleNamespace(run_id="r", confirm_unsent=False, reason="x", actor="u"))


if __name__ == "__main__":
    unittest.main()
