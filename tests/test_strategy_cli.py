"""Strategy CLI/API slice: the `factory strategy …` commands and `stage --brief` are thin wiring to the briefs slice
(`factory.strategy`, owned by a sibling worktree) and the execution slice's `dispatch.stage_brief`. `factory.strategy`
is imported lazily inside the handler so the CLI still loads without it during a split checkout; these tests inject a
fake module so the lazy import resolves, and assert the exact arguments each command passes through."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from factory import cli, dispatch


def run_cli(fn, cfg, conn, a):
    """A command's JSON output."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(cfg, conn, a)
    return json.loads(buf.getvalue())


def fake_strategy():
    """The briefs-slice interface, recorded so each test can assert the exact call it was handed."""
    mod = ModuleType("factory.strategy")
    mod.overview = mock.Mock(return_value={"briefs": [], "tickets": []})
    mod.get = mock.Mock(return_value={"id": 1, "state": "draft", "body": {"title": "t"}})
    mod.render = mock.Mock(return_value="# 1\n")
    mod.ready = mock.Mock(return_value=[])
    mod.create = mock.Mock(return_value={"id": 1, "state": "draft"})
    mod.groom = mock.Mock(return_value={"id": 1, "state": "draft"})
    mod.revise = mock.Mock(return_value={"id": 2, "state": "draft"})
    mod.approve = mock.Mock(return_value={"id": 1, "state": "approved"})
    mod.hold = mock.Mock(return_value={"id": 1, "state": "held"})
    mod.unhold = mock.Mock(return_value={"id": 1, "state": "approved"})
    return mod


@contextlib.contextmanager
def strategy_module():
    """Install the fake `factory.strategy` for the handler's lazy `from . import strategy`."""
    mod = fake_strategy()
    prev = sys.modules.get("factory.strategy")
    sys.modules["factory.strategy"] = mod
    try:
        yield mod
    finally:
        if prev is None:
            sys.modules.pop("factory.strategy", None)
        else:
            sys.modules["factory.strategy"] = prev


def a(**kw):
    return SimpleNamespace(**kw)


class StrategyCommands(unittest.TestCase):
    """Each `factory strategy …` command passes the exact interface arguments through to the briefs slice."""

    def test_list_is_overview(self):
        with strategy_module() as mod:
            out = run_cli(cli.cmd_strategy, "cfg", "conn", a(scmd="list"))
        mod.overview.assert_called_once_with("cfg", "conn")
        self.assertEqual(out, {"briefs": [], "tickets": []})

    def test_show_is_get_without_render(self):
        with strategy_module() as mod:
            out = run_cli(cli.cmd_strategy, "cfg", "conn", a(scmd="show", brief_id=9, render=False))
        mod.get.assert_called_once_with("conn", 9)
        mod.render.assert_not_called()
        self.assertEqual(out["id"], 1)

    def test_show_with_render_returns_brief_and_markdown(self):
        with strategy_module() as mod:
            out = run_cli(cli.cmd_strategy, "cfg", "conn", a(scmd="show", brief_id=9, render=True))
        mod.get.assert_called_once_with("conn", 9)
        mod.render.assert_called_once_with("conn", 9)
        self.assertEqual(set(out), {"brief", "render"})

    def test_groom_passes_identifiers_and_actor(self):
        with strategy_module() as mod:
            run_cli(cli.cmd_strategy, "cfg", "conn", a(scmd="groom", identifiers=["FIN-1", "FIN-2"], actor="user"))
        mod.groom.assert_called_once_with("cfg", "conn", ["FIN-1", "FIN-2"], "user")

    def test_create_without_body_is_deterministic(self):
        with strategy_module() as mod:
            run_cli(cli.cmd_strategy, "cfg", "conn", a(scmd="create", identifiers=["FIN-1"], body=None, actor="u"))
        mod.create.assert_called_once_with("cfg", "conn", ["FIN-1"], "u", body=None)

    def test_create_with_inline_body(self):
        body = {"title": "Fix", "outcome": "done", "acceptance": ["a"], "scope": ["s"]}
        with strategy_module() as mod:
            run_cli(cli.cmd_strategy, "cfg", "conn", a(scmd="create", identifiers=["FIN-1"], body=json.dumps(body), actor="u"))
        mod.create.assert_called_once_with("cfg", "conn", ["FIN-1"], "u", body=body)

    def test_revise_passes_body_and_reason(self):
        body = {"title": "Fix"}
        with strategy_module() as mod:
            run_cli(cli.cmd_strategy, "cfg", "conn", a(scmd="revise", brief_id=3, body=json.dumps(body),
                                                        reason="re-scope", actor="u"))
        mod.revise.assert_called_once_with("cfg", "conn", 3, body, "re-scope", "u")

    def test_approve_hold_unhold(self):
        with strategy_module() as mod:
            run_cli(cli.cmd_strategy, "cfg", "conn", a(scmd="approve", brief_id=5, actor="u"))
            run_cli(cli.cmd_strategy, "cfg", "conn", a(scmd="hold", brief_id=5, reason="wait", actor="u"))
            run_cli(cli.cmd_strategy, "cfg", "conn", a(scmd="unhold", brief_id=5, actor="u"))
        mod.approve.assert_called_once_with("cfg", "conn", 5, "u")
        mod.hold.assert_called_once_with("cfg", "conn", 5, "wait", "u")
        mod.unhold.assert_called_once_with("cfg", "conn", 5, "u")


class JsonBody(unittest.TestCase):
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


class StageBrief(unittest.TestCase):
    def test_brief_skips_ingest_and_calls_stage_brief(self):
        with mock.patch.object(dispatch, "stage_brief", create=True, return_value={"run_id": "b"}) as sb, \
             mock.patch.object(cli, "ingest") as ing:
            out = run_cli(cli.cmd_stage, "cfg", "conn", a(brief_id=7, identifiers=[], actor="u"))
        sb.assert_called_once_with("cfg", "conn", 7, "u")
        ing.assert_not_called()  # brief-backed stage never re-reads Linear
        self.assertEqual(out, {"run_id": "b"})

    def test_brief_rejects_ticket_identifiers(self):
        with self.assertRaises(dispatch.StageError):
            cli.cmd_stage("cfg", "conn", a(brief_id=7, identifiers=["FIN-1"], actor="u"))

    def test_legacy_stage_still_ingests_then_stages(self):
        with mock.patch.object(cli, "_repos_of", return_value=set()), \
             mock.patch.object(cli, "ingest") as ing, \
             mock.patch.object(dispatch, "stage", return_value={"run_id": "r"}) as st:
            out = run_cli(cli.cmd_stage, "cfg", "conn", a(brief_id=None, identifiers=["FIN-1"], actor="u"))
        ing.assert_called_once_with("cfg", "conn", only=set())
        st.assert_called_once_with("cfg", "conn", ["FIN-1"], "u")
        self.assertEqual(out, {"run_id": "r"})


if __name__ == "__main__":
    unittest.main()
