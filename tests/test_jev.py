"""Jev: the shared client contract (validated, sanitized, no fabrication), judgment guidance on open
plan/ask/review decisions (fingerprint reuse, rule matching, stale-rule drop), the plan-time investigation gate,
and learning decisions never earning rule approval."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import db, decide, dispatch, jev
from factory.config import ConfigError

SNAP = "2026-09-01T00:00:00Z"
CATEGORIES = ("investigate", "policy", "human", "unclear")


def ok_result(category="human", confidence=0.9):
    probs = {c: 0.0 for c in CATEGORIES}
    probs[category] = confidence
    rest = (1 - confidence) / (len(CATEGORIES) - 1)
    for c in CATEGORIES:
        if c != category:
            probs[c] = round(rest, 3)
    return {"status": "ok", "model": "jev-1.13.0",
            "answers": {"category": {"type": "choice", "choice": category,
                                     "probabilities": probs, "confidence": confidence}},
            "usage": {"input_tokens": 10, "output_tokens": 5}}


class Client(unittest.TestCase):
    """evaluate(): never raises, never fabricates, validates the response, sanitizes errors."""
    def setUp(self):
        self.cfg = SimpleNamespace(raw={"jev": {"enabled": True, "model": "jev-1.13.0", "timeout_seconds": 5}})

    def test_requires_an_explicitly_enabled_config(self):
        self.assertEqual(jev.evaluate(SimpleNamespace(raw={}), "s", {})["status"], "disabled")

    def test_a_missing_key_is_unavailable_without_any_call(self):
        with mock.patch("factory.jev.config.secret", side_effect=ConfigError("secret TYPESAFE_API_KEY not found")), \
                mock.patch("factory.jev.urllib.request.urlopen") as url:
            res = jev.evaluate(self.cfg, {"a": 1}, {})
        url.assert_not_called()
        self.assertEqual(res["status"], "unavailable")
        self.assertIn("TYPESAFE_API_KEY", res["error"])

    def test_ok_response_is_validated_and_returned(self):
        payload = {"model": "jev-1.13.0",
                   "answers": {"category": {"type": "choice", "choice": "human",
                                            "probabilities": {"human": 0.9, "unclear": 0.1}, "confidence": 0.9}},
                   "usage": {"input_tokens": 11, "output_tokens": 3}}
        seen = {}

        def urlopen(req, timeout):
            seen.update(body=req.data, auth=req.headers["Authorization"])
            cm = mock.MagicMock()
            cm.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            return cm

        with mock.patch("factory.jev.config.secret", return_value="k"), \
                mock.patch("factory.jev.urllib.request.urlopen", side_effect=urlopen) as url:
            res = jev.evaluate(self.cfg, {"decision": {"question": "q?"}}, jev._questions())
        url.assert_called_once()
        self.assertEqual(res, {"status": "ok", "model": "jev-1.13.0", "answers": payload["answers"],
                               "usage": {"input_tokens": 11, "output_tokens": 3}})
        self.assertEqual(seen["auth"], "Bearer k")
        body = json.loads(seen["body"])
        self.assertEqual(body["model"], "jev-1.13.0")
        self.assertEqual(body["questions"]["category"]["criteria"], jev._questions()["category"]["criteria"])

    def test_http_errors_are_sanitized(self):
        import urllib.error
        with mock.patch("factory.jev.config.secret", return_value="k"), \
                mock.patch("factory.jev.urllib.request.urlopen",
                           side_effect=urllib.error.HTTPError("u", 429, "too many", {}, None)):
            res = jev.evaluate(self.cfg, "s", jev._questions())
        self.assertEqual(res, {"status": "unavailable", "error": "typesafe api: HTTP 429"})

    def test_network_errors_carry_no_provider_detail(self):
        import urllib.error
        with mock.patch("factory.jev.config.secret", return_value="k"), \
                mock.patch("factory.jev.urllib.request.urlopen",
                           side_effect=urllib.error.URLError("connection refused; secret-abc leaked")):
            res = jev.evaluate(self.cfg, "s", jev._questions())
        self.assertEqual(res["status"], "unavailable")
        self.assertEqual(res["error"], "typesafe api: URLError")

    def bad(self, **over):
        payload = {"model": "jev-1.13.0",
                   "answers": {"category": {"type": "choice", "choice": "human",
                                            "probabilities": {"human": 0.9, "unclear": 0.1}, "confidence": 0.9}},
                   "usage": {"input_tokens": 1, "output_tokens": 1}}
        for path, v in over.items():
            obj = payload
            keys = path.split(".")
            for k in keys[:-1]:
                obj = obj[k]
            obj[keys[-1]] = v
        return payload

    def test_invalid_responses_are_unavailable_not_judgments(self):
        cases = {"answers.category.choice": "nope", "answers.category.confidence": 1.5,
                 "answers.category.probabilities.human": -0.1, "answers.category.type": "noul",
                 "answers": {}, "usage.input_tokens": "many"}
        for path, value in cases.items():
            with self.subTest(path=path), mock.patch("factory.jev.config.secret", return_value="k"), \
                    mock.patch("factory.jev.urllib.request.urlopen",
                               return_value=mock.MagicMock(**{
                                   "__enter__.return_value.read.return_value": json.dumps(self.bad(**{path: value})).encode()})):
                res = jev.evaluate(self.cfg, "s", jev._questions())
            self.assertEqual(res["status"], "unavailable", path)
            self.assertTrue(res["error"].startswith("typesafe api:"), path)


class Guidance(unittest.TestCase):
    """refresh(): stores guidance, reuses unchanged successes, retries failures, never touches learnings."""
    def setUp(self):
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        self.cfg = SimpleNamespace(raw={"jev": {"enabled": True}})
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
                       "VALUES ('d1','draft','[{\"repo\":\"r\",\"trunk_sha\":\"a\"}]','t',?)", (SNAP,))

    def plan_decision(self, question="Ship the feature or not?"):
        return decide.open_(self.c, "plan", question,
                            [decide.option("a", "Yes", "it lands"), decide.option("b", "No", "it does not")],
                            "a", "because", "t", run_id="d1")

    def seed_rule(self, body, keep_by="user"):
        self.c.execute("INSERT INTO learning(kind,scope,body,anchors_json,source,status,created_at) "
                       "VALUES ('house_rule','r',?,'[]','decision:1','active',?)", (body, SNAP))
        lid = self.c.execute("SELECT max(id) FROM learning").fetchone()[0]
        ld = decide.open_(self.c, "learning", "keep it?", [decide.option("keep", "Keep", "x"),
                                                           decide.option("reject", "Drop", "y")],
                          "keep", "why", "factory:learn", ref=str(lid))
        decide.choose(self.cfg, self.c, ld, "keep", keep_by)
        return lid

    def test_refresh_stores_guidance_and_reuses_an_unchanged_success(self):
        did = self.plan_decision()
        with mock.patch("factory.jev.evaluate", return_value=ok_result("human", 0.9)) as ev:
            res = jev.refresh(self.cfg, self.c)
        self.assertEqual(res, [{"decision": did, "status": "ok", "category": "human", "confidence": 0.9}])
        d = decide.one(self.c, did)
        self.assertEqual(d["jev"]["status"], "ok")
        self.assertEqual(d["jev"]["category"], "human")
        self.assertEqual(d["jev"]["model"], "jev-1.13.0")
        self.assertEqual(d["jev"]["focus"], "none")  # the options carry no differing consequence text
        self.assertTrue(d["jev"]["fingerprint"])
        self.assertEqual(d["jev"], d["detail"]["jev"])  # the same shape is persisted and served
        with mock.patch("factory.jev.evaluate", return_value=ok_result()) as ev2:
            self.assertEqual(jev.refresh(self.cfg, self.c), [])
        ev2.assert_not_called()  # unchanged success: no second call this tick

    def test_a_failed_call_is_visible_and_retried_on_the_next_tick(self):
        did = self.plan_decision()
        with mock.patch("factory.jev.evaluate",
                        return_value={"status": "unavailable", "error": "typesafe api: HTTP 500"}):
            jev.refresh(self.cfg, self.c)
        d = decide.one(self.c, did)
        self.assertEqual(d["jev"]["status"], "unavailable")
        self.assertEqual(d["jev"]["error"], "typesafe api: HTTP 500")
        with mock.patch("factory.jev.evaluate", return_value=ok_result()) as ev:
            jev.refresh(self.cfg, self.c)
        ev.assert_called_once()  # failures are never cached as judgments
        self.assertEqual(decide.one(self.c, did)["jev"]["status"], "ok")

    def test_a_changed_decision_is_rejudged(self):
        did = self.plan_decision()
        with mock.patch("factory.jev.evaluate", return_value=ok_result("human", 0.9)):
            jev.refresh(self.cfg, self.c)
        self.seed_rule("You choose “Yes” (2×)")  # a new eligible rule changes the judgment input
        with mock.patch("factory.jev.evaluate", return_value=ok_result("unclear", 0.6)) as ev:
            jev.refresh(self.cfg, self.c)
        ev.assert_called_once()
        self.assertEqual(decide.one(self.c, did)["jev"]["category"], "unclear")

    def test_an_answer_made_during_the_call_is_never_overwritten(self):
        did = self.plan_decision()

        def answer_it(cfg, state, questions):
            decide.choose(self.cfg, self.c, did, "a", "user")
            return ok_result()

        with mock.patch("factory.jev.evaluate", side_effect=answer_it):
            jev.refresh(self.cfg, self.c)
        raw = self.c.execute("SELECT detail_json FROM decision WHERE id=?", (did,)).fetchone()[0]
        self.assertNotIn("jev", json.loads(raw))

    def test_refresh_never_touches_learning_decisions(self):
        lid = self.seed_rule("You choose “Keep it” (2×)")
        ld = decide.open_(self.c, "learning", "keep it?", [decide.option("keep", "Keep", "x"),
                                                           decide.option("reject", "Drop", "y")],
                          "keep", "why", "factory:learn", ref=str(lid),
                          detail={"jev": {"relation": {"kind": "duplicate", "learning_id": 7}}})
        with mock.patch("factory.jev.evaluate") as ev:
            jev.refresh(self.cfg, self.c)
        ev.assert_not_called()
        raw = json.loads(self.c.execute("SELECT detail_json FROM decision WHERE id=?", (ld,)).fetchone()[0])
        self.assertEqual(raw["jev"], {"relation": {"kind": "duplicate", "learning_id": 7}})

    def test_an_executor_ask_stays_open_regardless_of_category(self):
        did = decide.open_(self.c, "ask", "q?", [decide.option("a", "A", "x"), decide.option("b", "B", "y")],
                           "a", "why", "executor", run_id="d1")
        with mock.patch("factory.jev.evaluate", return_value=ok_result("investigate", 0.95)):
            jev.refresh(self.cfg, self.c)
        d = decide.one(self.c, did)
        self.assertTrue(d["open"])
        self.assertEqual(d["jev"]["category"], "investigate")  # guidance, never an answer

    def test_policy_category_matches_a_human_kept_rule(self):
        q = "Allow preview origins too?"
        did = decide.open_(self.c, "plan", q,
                           [decide.option("prod", "Production console only", "x"),
                            decide.option("all", "Preview origins too", "y")], "prod", "why", "t", run_id="d1")
        body = f"{q} → Preview origins too, not Production console only: ship it"
        lid = self.seed_rule(body)
        with mock.patch("factory.jev.evaluate", return_value=ok_result("policy", 0.9)):
            jev.refresh(self.cfg, self.c)
        d = decide.one(self.c, did)
        self.assertEqual(d["jev"]["category"], "policy")
        self.assertEqual(d["jev"]["rule"], {"id": lid, "body": body, "option_id": "all"})

    def test_a_rule_the_factory_kept_is_not_an_approved_rule(self):
        q = "Allow preview origins too?"
        did = decide.open_(self.c, "plan", q,
                           [decide.option("prod", "Production console only", "x"),
                            decide.option("all", "Preview origins too", "y")], "prod", "why", "t", run_id="d1")
        self.seed_rule(f"{q} → Preview origins too, not Production console only: ship it", keep_by="factory:auto")
        with mock.patch("factory.jev.evaluate", return_value=ok_result("policy", 0.9)):
            jev.refresh(self.cfg, self.c)
        self.assertNotIn("rule", decide.one(self.c, did)["jev"])

    def test_reads_drop_a_stale_rule_claim(self):
        q = "Allow preview origins too?"
        did = decide.open_(self.c, "plan", q,
                           [decide.option("prod", "Production console only", "x"),
                            decide.option("all", "Preview origins too", "y")], "prod", "why", "t", run_id="d1")
        lid = self.seed_rule(f"{q} → Preview origins too, not Production console only: ship it")
        with mock.patch("factory.jev.evaluate", return_value=ok_result("policy", 0.9)):
            jev.refresh(self.cfg, self.c)
        self.assertIsNotNone(decide.one(self.c, did)["jev"]["rule"])
        self.c.execute("UPDATE learning SET status='expired' WHERE id=?", (lid,))  # anchors moved on
        d = decide.one(self.c, did)
        self.assertIsNone(d["jev"]["rule"])  # no stale approved-rule claim after it expires
        self.assertEqual(d["jev"]["status"], "ok")


class Focus(unittest.TestCase):
    def test_focus_is_the_first_aspect_where_options_differ(self):
        opts = [{"id": "a", "label": "A", "leads_to": "x", "changes": [], "result": "r", "cost": "5 min",
                 "risk": "none"},
                {"id": "b", "label": "B", "leads_to": "y", "changes": [], "result": "r", "cost": "5 min",
                 "risk": "full rescan"}]
        self.assertEqual(jev.focus({"kind": "plan", "options": opts}), "risk")
        opts[1]["risk"] = "none"
        opts[0]["changes"] = [{"step": "FIN-1/1", "becomes": "other"}]
        self.assertEqual(jev.focus({"kind": "plan", "options": opts}), "changes")
        opts[1]["changes"] = opts[0]["changes"]
        self.assertEqual(jev.focus({"kind": "plan", "options": opts}), "none")
        self.assertEqual(jev.focus({"kind": "ask", "options": opts}), "none")


class PlanGate(unittest.TestCase):
    """The investigation gate rejects before any mutation; a disabled or unsure Jev changes nothing."""
    def setUp(self):
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        self.cfg = SimpleNamespace(raw={"jev": {"enabled": True}})
        self.seed()

    def seed(self):
        self.c.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1',?,?,'unstarted',1,'{}')", (SNAP, SNAP))
        self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,repo,trunk_sha,kind,reason,evidence_json,"
                       "evidence_paths_json,created_at,created_by) VALUES ('i1',?,'r','a','valid','x',"
                       "'[{\"type\":\"file\",\"path\":\"f.py\",\"note\":\"n\"}]','[\"f.py\"]',?,'t')", (SNAP, SNAP))
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
                       "VALUES ('d1','draft','[{\"repo\":\"r\",\"trunk_sha\":\"a\"}]','t',?)", (SNAP,))
        self.c.execute("INSERT INTO dispatch_ticket(run_id,issue_id,identifier,snapshot_updated_at,verdict_id) "
                       "VALUES ('d1','i1','FIN-1',?,1)", (SNAP,))

    def payload(self):
        return [{"id": "root", "title": "t", "result": "r", "recommend": "approve", "why": "w"},
                {"id": "FIN-1", "result": "r"},
                {"id": "FIN-1/1", "title": "step", "result": "r"},
                {"question": "Which flag is set in the code?", "key": "flag", "on": "root", "now": "n",
                 "options": [{"id": "x", "label": "X", "leads_to": "x", "changes": [], "result": "r"},
                             {"id": "y", "label": "Y", "leads_to": "y", "changes": [], "result": "r"}],
                 "recommend": "x", "why": "w"}]

    def test_a_sure_pure_investigation_question_is_refused_before_anything_is_written(self):
        with mock.patch("factory.jev.evaluate", return_value=ok_result("investigate", 0.9)) as ev:
            with self.assertRaises(dispatch.StageError) as ctx:
                dispatch.plan(self.cfg, self.c, "d1", self.payload())
        self.assertIn("investigate in the code", str(ctx.exception))
        self.assertIn("'flag'", str(ctx.exception))  # names the question: actionable for the planner
        d = self.c.execute("SELECT state, planned_at FROM dispatch WHERE run_id='d1'").fetchone()
        self.assertEqual((d["state"], d["planned_at"]), ("draft", None))
        self.assertEqual(self.c.execute("SELECT count(*) FROM dispatch_step").fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT count(*) FROM decision").fetchone()[0], 0)
        ev.assert_called_once()  # the review is not judged once the gate fires

    def test_an_unsure_investigation_commits_with_guidance(self):
        with mock.patch("factory.jev.evaluate", return_value=ok_result("investigate", 0.7)):
            res = dispatch.plan(self.cfg, self.c, "d1", self.payload())
        self.assertEqual(res["questions"], 1)
        q = next(d for d in decide.rows(self.c, "d1") if d["kind"] == "plan")
        self.assertEqual(q["jev"]["category"], "investigate")
        self.assertEqual(q["jev"]["confidence"], 0.7)

    def test_plan_commits_with_guidance_when_jev_succeeds(self):
        with mock.patch("factory.jev.evaluate", return_value=ok_result("human", 0.9)):
            res = dispatch.plan(self.cfg, self.c, "d1", self.payload())
        self.assertEqual(res["recommend"], "approve")
        by_kind = {d["kind"]: d for d in decide.rows(self.c, "d1")}
        self.assertEqual(by_kind["plan"]["jev"]["status"], "ok")
        self.assertEqual(by_kind["review"]["jev"]["status"], "ok")

    def test_plan_commits_normally_when_jev_is_unavailable(self):
        with mock.patch("factory.jev.evaluate",
                        return_value={"status": "unavailable", "error": "typesafe api: HTTP 500"}):
            dispatch.plan(self.cfg, self.c, "d1", self.payload())
        self.assertIsNotNone(self.c.execute("SELECT planned_at FROM dispatch WHERE run_id='d1'").fetchone()[0])
        by_kind = {d["kind"]: d for d in decide.rows(self.c, "d1")}
        self.assertEqual(by_kind["plan"]["jev"]["status"], "unavailable")
        self.assertEqual(by_kind["review"]["jev"]["status"], "unavailable")

    def test_plan_with_jev_disabled_has_no_guidance_and_no_gate(self):
        cfg = SimpleNamespace(repos={}, raw={})
        dispatch.plan(cfg, self.c, "d1", self.payload())
        for d in decide.rows(self.c, "d1"):
            self.assertIsNone(d["jev"])


class LearningTier(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        self.cfg = SimpleNamespace(repos={}, raw={})

    def test_learnings_never_earn_auto_and_are_not_swept(self):
        opts = [decide.option("keep", "Keep", "x"), decide.option("reject", "Drop", "y")]
        for _ in range(decide.EARNED_AFTER):  # straight human ★ answers: every other kind would have earned auto
            did = decide.open_(self.c, "learning", "keep it?", opts, "keep", "why", "factory:learn", ref="9")
            decide.choose(self.cfg, self.c, did, "keep", "user")
        did = decide.open_(self.c, "learning", "keep it?", opts, "keep", "why", "factory:learn", ref="9")
        self.assertEqual(decide.one(self.c, did)["tier"], "digest")
        # a legacy earned-auto learning row (tier frozen at insert): reads as digest, waits for the user, never swept
        legacy = self.c.execute("INSERT INTO decision(run_id,node_id,kind,ref,question,options_json,recommended,why,"
                                "detail_json,created_at,created_by,tier,due_at) VALUES (NULL,'root','learning','9',"
                                "'legacy auto learning',?,?,?,'{}',?,'factory:learn','auto',?)",
                                (json.dumps(opts), "keep", "why", SNAP, SNAP)).lastrowid
        d = decide.one(self.c, legacy)
        self.assertEqual(d["tier"], "digest")
        self.assertIsNone(d["due_at"])
        self.assertIsNone(d["deadline"])  # waits for the user, not "the factory takes ★"
        self.assertEqual(decide.sweep(self.cfg, self.c), [])
        self.assertTrue(decide.one(self.c, legacy)["open"])


if __name__ == "__main__":
    unittest.main()
