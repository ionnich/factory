"""Jev: the shared client contract (validated, sanitized, no fabrication, bounded), judgment guidance on open
plan/ask/review decisions (one batched typed-Choice call: category, each rule's own option Choice, focus;
fingerprint reuse, a budgeted refresh that never starves later decisions, scoped stale-claim removal), the
plan-time investigation gate, and learning decisions never earning rule approval."""
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import db, decide, dispatch, jev
from factory.config import ConfigError

SNAP = "2026-09-01T00:00:00Z"


def choice_answer(questions, qid, choice, confidence=0.9):
    """A valid typed answer for qid: the exact criteria keys, normalized, peaking at `choice`."""
    criteria = list(questions[qid]["criteria"])
    probs = {c: confidence if c == choice else (1 - confidence) / (len(criteria) - 1) for c in criteria}
    return {"type": "choice", "choice": choice, "probabilities": probs, "confidence": confidence}


def ok_result(category="human", confidence=0.9, rules=None, focus=None):
    """evaluate() stub: answers every question asked — each rule's own question (rule_<id>) with
    rules[id] = (option id, confidence), else none — and the answers pass the client's own validation (complete,
    normalized probability maps), so assess sees a realistic batched judgment."""
    def stub(cfg, state, questions, timeout=None):
        answers = {"category": choice_answer(questions, "category", category, confidence)}
        for qid in questions:
            if qid.startswith("rule_"):
                answers[qid] = choice_answer(questions, qid, *(rules or {}).get(int(qid[5:]), ("none", 0.9)))
        if "focus" in questions:
            answers["focus"] = choice_answer(questions, "focus", focus or "none", 0.9)
        res = {"status": "ok", "model": "jev-1.13.0", "answers": answers,
               "usage": {"input_tokens": 10, "output_tokens": 5}}
        jev._validate(res, questions)
        return res
    return stub


class Client(unittest.TestCase):
    """evaluate(): never raises, never fabricates, validates the response, never echoes a secret or provider text."""
    KEY = "tsk-live-secret-key"

    def setUp(self):
        self.cfg = SimpleNamespace(raw={"jev": {"enabled": True, "model": "jev-1.13.0", "timeout_seconds": 5}})
        self.qs = jev._questions({"kind": "plan", "options": []}, [])
        self.base = {"model": "jev-1.13.0",
                     "answers": {"category": {"type": "choice", "choice": "human",
                                              "probabilities": {"human": 0.9, "unclear": 0.1,
                                                                "investigate": 0.0, "policy": 0.0},
                                              "confidence": 0.9}},
                     "usage": {"input_tokens": 11, "output_tokens": 3}}

    def assertUnavailable(self, res, *leaks):
        """A failure: unavailable, no judgment, and neither the key nor any provider/secret text in it."""
        self.assertEqual(res["status"], "unavailable")
        self.assertNotIn("answers", res)
        blob = json.dumps(res)
        for leak in (self.KEY, *leaks):
            self.assertNotIn(leak, blob)

    def test_requires_an_explicitly_enabled_config(self):
        self.assertEqual(jev.evaluate(SimpleNamespace(raw={}), "s", {})["status"], "disabled")

    def test_a_missing_key_is_unavailable_without_any_call(self):
        with mock.patch("factory.jev.config.secret", side_effect=ConfigError("secret TYPESAFE_API_KEY not found")), \
                mock.patch("factory.jev.urllib.request.urlopen") as url:
            res = jev.evaluate(self.cfg, {"a": 1}, {})
        url.assert_not_called()
        self.assertUnavailable(res)

    def test_an_unreadable_secret_is_unavailable_and_never_echoed(self):
        for exc in (OSError("TYPESAFE_API_KEY=tsk-from-env-file unreadable"),
                    UnicodeDecodeError("utf-8", b"tsk-env-bytes\xff", 13, 14, "tsk-decode-reason")):
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch("factory.jev.config.secret", side_effect=exc), \
                    mock.patch("factory.jev.urllib.request.urlopen") as url:
                res = jev.evaluate(self.cfg, {"a": 1}, {})
                url.assert_not_called()
                self.assertUnavailable(res, "tsk-from-env-file", "tsk-env-bytes", "tsk-decode-reason")

    def test_ok_response_is_validated_and_returned(self):
        seen = {}

        def urlopen(req, timeout):
            seen.update(auth=req.headers["Authorization"])
            return self.respond(self.base)

        with mock.patch("factory.jev.config.secret", return_value=self.KEY), \
                mock.patch("factory.jev.urllib.request.urlopen", side_effect=urlopen) as url:
            res = jev.evaluate(self.cfg, {"decision": {"question": "q?"}}, self.qs)
        url.assert_called_once()
        self.assertEqual(res, {"status": "ok", "model": "jev-1.13.0", "answers": self.base["answers"],
                               "usage": {"input_tokens": 11, "output_tokens": 3}})
        self.assertEqual(seen["auth"], f"Bearer {self.KEY}")

    def test_a_call_never_waits_past_the_callers_budget_or_its_timeout(self):
        seen = []
        with mock.patch("factory.jev.config.secret", return_value=self.KEY), \
                mock.patch("factory.jev.urllib.request.urlopen",
                           side_effect=lambda req, timeout: seen.append(timeout) or self.respond(self.base)):
            for left in (2.5, 60):
                self.assertEqual(jev.evaluate(self.cfg, "s", self.qs, timeout=left)["status"], "ok")
        self.assertEqual(seen, [2.5, 5])  # the remaining budget, never beyond timeout_seconds (5)

    def test_http_errors_are_unavailable_without_the_provider_body(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 429, "tsk-provider-reason", {},
                                     io.BytesIO(b'{"error": "key tsk-provider-body rejected"}'))
        with mock.patch("factory.jev.config.secret", return_value=self.KEY), \
                mock.patch("factory.jev.urllib.request.urlopen", side_effect=err):
            res = jev.evaluate(self.cfg, "s", self.qs)
        self.assertUnavailable(res, "tsk-provider-reason", "tsk-provider-body")

    def test_network_and_protocol_errors_are_unavailable_without_provider_detail(self):
        import http.client
        import urllib.error
        for exc in (urllib.error.URLError("connection refused; tsk-network-detail"),
                    http.client.IncompleteRead(b"tsk-partial-body")):
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch("factory.jev.config.secret", return_value=self.KEY), \
                    mock.patch("factory.jev.urllib.request.urlopen", side_effect=exc):
                self.assertUnavailable(jev.evaluate(self.cfg, "s", self.qs), "tsk-network-detail", "tsk-partial-body")

    def test_an_undecodable_response_is_unavailable_not_a_judgment(self):
        with mock.patch("factory.jev.config.secret", return_value=self.KEY), \
                mock.patch("factory.jev.urllib.request.urlopen",
                           return_value=mock.MagicMock(**{
                               "__enter__.return_value.read.return_value": b"\xff\xfe"})):
            res = jev.evaluate(self.cfg, "s", self.qs)
        self.assertUnavailable(res)

    def test_unserializable_state_is_unavailable_without_a_call_or_an_echo(self):
        with mock.patch("factory.jev.config.secret", return_value=self.KEY), \
                mock.patch("factory.jev.urllib.request.urlopen") as url:
            res = jev.evaluate(self.cfg, {"bad": object(), "note": "tsk-state-text"}, self.qs)
        url.assert_not_called()
        self.assertUnavailable(res, "tsk-state-text")

    def bad(self, **over):
        payload = json.loads(json.dumps(self.base))
        for path, v in over.items():
            obj = payload
            keys = path.split(".")
            for k in keys[:-1]:
                obj = obj[k]
            obj[keys[-1]] = v
        return payload

    def respond(self, payload):
        return mock.MagicMock(**{"__enter__.return_value.read.return_value": json.dumps(payload).encode()})

    def evaluate_bad(self, payload):
        with mock.patch("factory.jev.config.secret", return_value=self.KEY), \
                mock.patch("factory.jev.urllib.request.urlopen", return_value=self.respond(payload)):
            return jev.evaluate(self.cfg, "s", self.qs)

    def test_invalid_responses_are_unavailable_not_judgments(self):
        cases = {"answers.category.choice": "tsk-provider-choice", "answers.category.confidence": 1.5,
                 "answers.category.probabilities.human": -0.1, "answers.category.type": "noul",
                 "answers": {}, "usage.input_tokens": "many"}
        for path, value in cases.items():
            with self.subTest(path=path):
                self.assertUnavailable(self.evaluate_bad(self.bad(**{path: value})), "tsk-provider-choice")

    def test_probability_maps_must_be_exact_normalized_and_peak_at_the_choice(self):
        def payload(probs, choice="human"):
            p = json.loads(json.dumps(self.base))
            p["answers"]["category"]["probabilities"] = probs
            p["answers"]["category"]["choice"] = choice
            return p

        bads = {
            "missing key": payload({"human": 0.9, "unclear": 0.1, "investigate": 0.0}),
            "unknown key": payload({"human": 0.9, "unclear": 0.1, "investigate": 0.0, "policy": 0.0,
                                    "extra": 0.0}),
            "not normalized": payload({"human": 0.4, "unclear": 0.1, "investigate": 0.0, "policy": 0.0}),
            "choice not maximum": payload({"human": 0.1, "unclear": 0.7, "investigate": 0.1, "policy": 0.1}),
            "non finite": payload({"human": float("inf"), "unclear": 0.1, "investigate": 0.0, "policy": 0.0}),
        }
        for name, p in bads.items():
            with self.subTest(name=name):
                self.assertUnavailable(self.evaluate_bad(p))
        # a tie for the maximum is fine
        ok = payload({"human": 0.5, "unclear": 0.5, "investigate": 0.0, "policy": 0.0})
        self.assertEqual(self.evaluate_bad(ok)["status"], "ok")


class Guidance(unittest.TestCase):
    """refresh(): stores guidance, reuses unchanged successes, retries failures, never touches learnings."""
    def setUp(self):
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        self.cfg = SimpleNamespace(raw={"jev": {"enabled": True}})
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
                       "VALUES ('d1','draft','[{\"repo\":\"r\",\"trunk_sha\":\"a\"}]','t',?)", (SNAP,))

    def plan_decision(self, question="Ship the feature or not?", node_id="root", options=None):
        options = options or [decide.option("a", "Yes", "it lands"), decide.option("b", "No", "it does not")]
        return decide.open_(self.c, "plan", question, options, "a", "because", "t", run_id="d1",
                            node_id=node_id)

    def seed_rule(self, body, scope="r", keep_by="user"):
        self.c.execute("INSERT INTO learning(kind,scope,body,anchors_json,source,status,created_at) "
                       "VALUES ('house_rule',?,?,'[]',?,'active',?)", (scope, body, f"theme:{body}", SNAP))
        lid = self.c.execute("SELECT max(id) FROM learning").fetchone()[0]
        ld = decide.open_(self.c, "learning", "keep it?", [decide.option("keep", "Keep", "x"),
                                                           decide.option("reject", "Drop", "y")],
                          "keep", "why", "factory:learn", ref=str(lid))
        decide.choose(self.cfg, self.c, ld, "keep", keep_by)
        return lid

    def test_refresh_stores_guidance_and_reuses_an_unchanged_success(self):
        did = self.plan_decision()
        with mock.patch("factory.jev.evaluate", side_effect=ok_result("human", 0.9)) as ev:
            res = jev.refresh(self.cfg, self.c)
        self.assertEqual(res, [{"decision": did, "status": "ok", "category": "human", "confidence": 0.9}])
        d = decide.one(self.c, did)
        self.assertEqual(d["jev"]["status"], "ok")
        self.assertEqual(d["jev"]["category"], "human")
        self.assertEqual(jev.stored(self.c, did), d["jev"])  # the stored payload is what reads serve
        detail = json.loads(self.c.execute("SELECT detail_json FROM decision WHERE id=?", (did,)).fetchone()[0])
        self.assertNotIn("jev", detail)  # guidance never lives inside detail_json
        with mock.patch("factory.jev.evaluate", side_effect=ok_result()) as ev2:
            self.assertEqual(jev.refresh(self.cfg, self.c), [])
        ev2.assert_not_called()  # unchanged success: no second call this tick

    def test_a_failed_call_is_visible_and_retried_on_the_next_tick(self):
        did = self.plan_decision()
        with mock.patch("factory.jev.evaluate",
                        return_value={"status": "unavailable", "error": "typesafe api: HTTP 500"}):
            jev.refresh(self.cfg, self.c)
        d = decide.one(self.c, did)
        self.assertEqual(d["jev"]["status"], "unavailable")
        with mock.patch("factory.jev.evaluate", side_effect=ok_result()) as ev:
            jev.refresh(self.cfg, self.c)
        ev.assert_called_once()  # failures are never cached as judgments
        self.assertEqual(decide.one(self.c, did)["jev"]["status"], "ok")

    def test_a_budgeted_pass_caps_each_call_and_never_starves_later_decisions(self):
        for i in range(3):
            self.plan_decision(f"Question {i}?")
        clock, calls, ends = [0.0], [], []

        def hang(cfg, state, questions, timeout=None):  # a hung call burns all the time it is allowed
            calls.append(state["decision"]["question"])
            clock[0] += timeout
            return {"status": "unavailable", "error": "typesafe api: TimeoutError"}

        with mock.patch.object(jev.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch("factory.jev.evaluate", side_effect=hang):
            for _ in range(4):  # four propose ticks
                clock[0] = 0.0
                jev.refresh(self.cfg, self.c, budget=10)
                ends.append(clock[0])
        self.assertEqual(ends, [10.0] * 4)  # one hung call per pass, capped to what was left of the budget
        # the decision whose call just failed waits behind the rest: each gets its turn before any retry
        self.assertEqual(calls, ["Question 0?", "Question 1?", "Question 2?", "Question 0?"])

    def test_a_changed_decision_is_rejudged(self):
        did = self.plan_decision()
        with mock.patch("factory.jev.evaluate", side_effect=ok_result("human", 0.9)):
            jev.refresh(self.cfg, self.c)
        self.seed_rule("You choose “Yes” (2×)")  # a new eligible rule changes the judgment input
        with mock.patch("factory.jev.evaluate", side_effect=ok_result("unclear", 0.6)) as ev:
            jev.refresh(self.cfg, self.c)
        ev.assert_called_once()
        self.assertEqual(decide.one(self.c, did)["jev"]["category"], "unclear")

    def test_a_model_change_is_rejudged(self):
        did = self.plan_decision()
        with mock.patch("factory.jev.evaluate", side_effect=ok_result("human", 0.9)):
            jev.refresh(self.cfg, self.c)
        self.cfg.raw["jev"]["model"] = "jev-2.0.0"  # the fingerprint covers the model
        with mock.patch("factory.jev.evaluate", side_effect=ok_result("unclear", 0.6)) as ev:
            jev.refresh(self.cfg, self.c)
        ev.assert_called_once()

    def test_an_answer_made_during_the_call_is_never_overwritten(self):
        did = self.plan_decision()

        def answer_it(cfg, state, questions, timeout=None):
            decide.choose(self.cfg, self.c, did, "a", "user")
            return ok_result()(cfg, state, questions)

        with mock.patch("factory.jev.evaluate", side_effect=answer_it):
            jev.refresh(self.cfg, self.c)
        self.assertIsNone(jev.stored(self.c, did))  # answered while judging: never stored

    def test_refresh_never_touches_learning_decisions(self):
        lid = self.seed_rule("You choose “Keep it” (2×)")
        ld = decide.open_(self.c, "learning", "keep it?", [decide.option("keep", "Keep", "x"),
                                                           decide.option("reject", "Drop", "y")],
                          "keep", "why", "factory:learn", ref=str(lid))
        advice = {"status": "ok", "relation": {"kind": "duplicate", "learning_id": 7}, "group": "learning:1"}
        self.assertTrue(jev.store(self.c, ld, advice))
        with mock.patch("factory.jev.evaluate") as ev:
            jev.refresh(self.cfg, self.c)
        ev.assert_not_called()
        self.assertEqual(jev.stored(self.c, ld), advice)  # the learning slice's metadata is never overwritten

    def test_an_executor_ask_stays_open_regardless_of_category(self):
        did = decide.open_(self.c, "ask", "q?", [decide.option("a", "A", "x"), decide.option("b", "B", "y")],
                           "a", "why", "executor", run_id="d1")
        with mock.patch("factory.jev.evaluate", side_effect=ok_result("investigate", 0.95, focus="result")):
            jev.refresh(self.cfg, self.c)
        d = decide.one(self.c, did)
        self.assertTrue(d["open"])
        self.assertEqual(d["jev"]["category"], "investigate")  # guidance, never an answer
        self.assertEqual(d["jev"]["focus"], "result")  # the ask's differing leads_to is its consequence

    def test_policy_category_maps_a_semantically_matching_rule(self):
        q = "Allow preview origins too?"
        did = decide.open_(self.c, "plan", q,
                           [decide.option("prod", "Production console only", "x"),
                            decide.option("all", "Preview origins too", "y")], "prod", "why", "t", run_id="d1")
        body = "Console access already reaches preview environments; production stays unchanged."
        lid = self.seed_rule(body)
        with mock.patch("factory.jev.evaluate",
                        side_effect=ok_result("policy", 0.9, rules={lid: ("all", 0.9)})):
            jev.refresh(self.cfg, self.c)
        d = decide.one(self.c, did)
        self.assertEqual(d["jev"]["category"], "policy")
        self.assertEqual(d["jev"]["rule"], {"id": lid, "body": body, "option_id": "all"})

    def test_a_low_confidence_rule_choice_emits_no_rule(self):
        q = "Allow preview origins too?"
        did = decide.open_(self.c, "plan", q,
                           [decide.option("prod", "Production console only", "x"),
                            decide.option("all", "Preview origins too", "y")], "prod", "why", "t", run_id="d1")
        lid = self.seed_rule("Console access already reaches preview environments.")
        with mock.patch("factory.jev.evaluate",
                        side_effect=ok_result("policy", 0.9, rules={lid: ("all", 0.5)})):
            jev.refresh(self.cfg, self.c)
        d = decide.one(self.c, did)
        self.assertEqual(d["jev"]["category"], "policy")
        self.assertNotIn("rule", d["jev"])  # an unsure rule choice is never a rule claim

    def test_a_rule_the_factory_kept_is_not_an_approved_rule(self):
        q = "Allow preview origins too?"
        did = decide.open_(self.c, "plan", q,
                           [decide.option("prod", "Production console only", "x"),
                            decide.option("all", "Preview origins too", "y")], "prod", "why", "t", run_id="d1")
        self.seed_rule("Console access already reaches preview environments.", keep_by="factory:auto")
        with mock.patch("factory.jev.evaluate", side_effect=ok_result("policy", 0.9)):
            jev.refresh(self.cfg, self.c)
        self.assertNotIn("rule", decide.one(self.c, did)["jev"])

    def test_equivalent_rules_each_match_on_their_own_confidence(self):
        # Two kept rules say the same thing. Each is its own question quoting its rule (the model never sees a
        # question id), all in one call, so neither dilutes the other's confidence the way one joint choice did.
        opts = [decide.option("prod", "Production console only", "x"),
                decide.option("all", "Preview origins too", "y")]
        bodies = ["Console access already reaches preview environments.", "Preview environments get the console."]
        older, newer = [self.seed_rule(b) for b in bodies]
        calls = []

        def judged(rules):
            def stub(cfg, state, questions, timeout=None):
                calls.append(questions)
                return ok_result("policy", 0.9, rules=rules)(cfg, state, questions)
            return stub

        first = decide.open_(self.c, "plan", "Allow preview origins too?", opts, "prod", "why", "t", run_id="d1")
        with mock.patch("factory.jev.evaluate", side_effect=judged({older: ("all", 0.9), newer: ("all", 0.97)})):
            jev.refresh(self.cfg, self.c)
        self.assertEqual(len(calls), 1)  # the category and both rules' questions in one call
        for lid, body in zip((older, newer), bodies):
            self.assertIn(body, calls[0][f"rule_{lid}"]["instructions"])
        # both sure of the same option: the most confident rule is the citation
        self.assertEqual(decide.one(self.c, first)["jev"]["rule"],
                         {"id": newer, "body": bodies[1], "option_id": "all"})
        tie = decide.open_(self.c, "plan", "Open previews to the console?", opts, "prod", "why", "t", run_id="d1")
        with mock.patch("factory.jev.evaluate", side_effect=judged({older: ("all", 0.9), newer: ("all", 0.9)})):
            jev.refresh(self.cfg, self.c)
        self.assertEqual(decide.one(self.c, tie)["jev"]["rule"]["id"], older)  # a tie cites the oldest rule, stably

    def test_sure_rules_that_disagree_cite_no_rule(self):
        did = decide.open_(self.c, "plan", "Allow preview origins too?",
                           [decide.option("prod", "Production console only", "x"),
                            decide.option("all", "Preview origins too", "y")], "prod", "why", "t", run_id="d1")
        only_prod = self.seed_rule("Preview environments never get the console.")
        allow = self.seed_rule("Console access already reaches preview environments.")
        with mock.patch("factory.jev.evaluate", side_effect=ok_result(
                "policy", 0.9, rules={only_prod: ("prod", 0.95), allow: ("all", 0.92)})):
            jev.refresh(self.cfg, self.c)
        self.assertNotIn("rule", decide.one(self.c, did)["jev"])  # neither sure rule resolves it over the other

    def test_reads_drop_a_stale_rule_claim_and_its_policy_category(self):
        q = "Allow preview origins too?"
        did = decide.open_(self.c, "plan", q,
                           [decide.option("prod", "Production console only", "x"),
                            decide.option("all", "Preview origins too", "y")], "prod", "why", "t", run_id="d1")
        lid = self.seed_rule("Console access already reaches preview environments.")
        with mock.patch("factory.jev.evaluate",
                        side_effect=ok_result("policy", 0.9, rules={lid: ("all", 0.9)})):
            jev.refresh(self.cfg, self.c)
        self.assertIsNotNone(decide.one(self.c, did)["jev"]["rule"])
        self.c.execute("UPDATE learning SET status='expired' WHERE id=?", (lid,))  # anchors moved on
        d = decide.one(self.c, did)
        self.assertNotIn("rule", d["jev"])  # no stale approved-rule claim after it expires
        self.assertEqual(d["jev"]["category"], "unclear")  # the policy claim left with its rule
        self.assertEqual(d["jev"]["status"], "ok")

    def test_reads_drop_a_stale_relation_and_its_group_together(self):
        did = self.plan_decision()
        self.c.execute("INSERT INTO learning(kind,scope,body,anchors_json,source,status,created_at) "
                       "VALUES ('house_rule','r','b','[]','decision:1','proposed',?)", (SNAP,))
        lid = self.c.execute("SELECT max(id) FROM learning").fetchone()[0]
        advice = {"status": "ok", "relation": {"kind": "duplicate", "learning_id": lid, "body": "b"},
                  "group": "learning:1"}
        self.assertTrue(jev.store(self.c, did, advice))
        self.assertEqual(jev.served(advice, self.c, [])["group"], "learning:1")
        self.c.execute("UPDATE learning SET status='expired' WHERE id=?", (lid,))
        for shown in (decide.one(self.c, did)["jev"], jev.served(advice, self.c, [])):
            self.assertNotIn("relation", shown)  # no relation to a gone learning
            self.assertNotIn("group", shown)
        # a group never outlives its relation, however the relation went missing
        self.assertNotIn("group", jev.served({"status": "ok", "group": "learning:1"}, self.c, []))

    def test_a_rewritten_or_relocated_relation_target_drops_relation_and_group(self):
        did = self.plan_decision()
        self.c.execute("INSERT INTO learning(kind,scope,body,anchors_json,source,status,created_at) "
                       "VALUES ('house_rule','r','b','[]','decision:1','proposed',?)", (SNAP,))
        lid = self.c.execute("SELECT max(id) FROM learning").fetchone()[0]
        advice = {"status": "ok", "relation": {"kind": "duplicate", "learning_id": lid, "body": "b"},
                  "group": "learning:1"}
        self.assertTrue(jev.store(self.c, did, advice))
        for change in ("body='rewritten'", "scope='other'"):  # each alone makes the judged target stale
            with self.subTest(change=change):
                self.c.execute("UPDATE learning SET body='b', scope='r' WHERE id=?", (lid,))
                self.assertEqual(decide.one(self.c, did)["jev"]["group"], "learning:1")
                self.c.execute(f"UPDATE learning SET {change} WHERE id=?", (lid,))
                shown = decide.one(self.c, did)["jev"]
                self.assertNotIn("relation", shown)
                self.assertNotIn("group", shown)

    def test_a_learning_decision_reads_its_relation_within_its_learnings_repo(self):
        lids = []
        for body in ("older fact", "proposed fact"):
            self.c.execute("INSERT INTO learning(kind,scope,body,anchors_json,source,status,created_at) "
                           "VALUES ('pitfall','r',?,'[]',?,'proposed',?)", (body, f"decision:{body}", SNAP))
            lids.append(self.c.execute("SELECT max(id) FROM learning").fetchone()[0])
        target, proposal = lids
        ld = decide.open_(self.c, "learning", "keep it?", [decide.option("keep", "Keep", "x"),
                                                           decide.option("reject", "Drop", "y")],
                          "keep", "why", "factory:learn", ref=str(proposal))
        self.assertTrue(jev.store(self.c, ld, {"status": "ok", "group": f"learning:{target}", "relation": {
            "kind": "duplicate", "learning_id": target, "body": "older fact"}}))
        self.assertEqual(decide.one(self.c, ld)["jev"]["group"], f"learning:{target}")
        self.c.execute("UPDATE learning SET scope='other' WHERE id=?", (target,))  # relocated to another repo
        shown = decide.one(self.c, ld)["jev"]
        self.assertNotIn("relation", shown)
        self.assertNotIn("group", shown)

    def test_a_stale_group_root_drops_the_group_but_keeps_a_current_relation(self):
        lids = []
        for body in ("root fact", "target fact", "proposed fact"):
            self.c.execute("INSERT INTO learning(kind,scope,body,anchors_json,source,status,created_at) "
                           "VALUES ('pitfall','r',?,'[]',?,'proposed',?)", (body, f"decision:{body}", SNAP))
            lids.append(self.c.execute("SELECT max(id) FROM learning").fetchone()[0])
        root, target, proposal = lids
        ld = decide.open_(self.c, "learning", "keep it?", [decide.option("keep", "Keep", "x"),
                                                           decide.option("reject", "Drop", "y")],
                          "keep", "why", "factory:learn", ref=str(proposal))
        relation = {"kind": "supports", "learning_id": target, "body": "target fact"}
        self.assertTrue(jev.store(self.c, ld, {"status": "ok", "relation": relation, "group": f"learning:{root}"}))
        decide.choose(self.cfg, self.c, ld, "keep", "user")  # answered: this advice is never refreshed again
        self.assertEqual(decide.one(self.c, ld)["jev"]["group"], f"learning:{root}")
        for change in ("status='rejected'", "status='expired'", "scope='other'"):
            with self.subTest(change=change):
                self.c.execute("UPDATE learning SET status='proposed', scope='r' WHERE id=?", (root,))
                self.c.execute(f"UPDATE learning SET {change} WHERE id=?", (root,))
                shown = decide.one(self.c, ld)["jev"]
                self.assertNotIn("group", shown)  # no group under a root since rejected, expired or relocated
                self.assertEqual(shown["relation"], relation)  # while the direct relation still stands
        self.assertEqual(jev.stored(self.c, ld)["group"], f"learning:{root}")  # filtered for display, still stored
        self.c.execute("UPDATE learning SET status='proposed', scope='r' WHERE id=?", (root,))
        # not learn's learning:<id> form (a leading zero, a suffix, an int), or an id past SQLite's: never a crash
        for group in (f"learning:0{root}", f"learning:{root}x", root, "learning:" + "9" * 25):
            with self.subTest(group=group):
                shown = jev.served({"status": "ok", "relation": relation, "group": group}, self.c, [], {"r"})
                self.assertNotIn("group", shown)
                self.assertEqual(shown["relation"], relation)

    def test_the_newest_kept_rules_are_offered_when_there_are_more_than_fit(self):
        lids = [self.seed_rule(f"Rule number {i}") for i in range(jev.MAX_RULES + 1)]
        seen = {}

        def stub(cfg, state, questions, timeout=None):
            seen.update(state=state)
            return ok_result()(cfg, state, questions)

        self.plan_decision()
        with mock.patch("factory.jev.evaluate", side_effect=stub):
            jev.refresh(self.cfg, self.c)
        # a new rule is never crowded out by older ones
        self.assertEqual({r["id"] for r in seen["state"]["rules"]}, set(lids[1:]))

    def test_a_cross_repo_rule_is_never_offered_to_a_ticket_question(self):
        # d1 covers r and r2; the question sits on ticket T in r2; the rule lives in r.
        self.c.execute("INSERT INTO linear_snapshot(issue_id,identifier,updated_at,fetched_at,state_type,in_scope,"
                       "raw_json) VALUES ('i1','T',?,?,'unstarted',1,'{}')", (SNAP, SNAP))
        self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,repo,trunk_sha,kind,reason,evidence_json,"
                       "evidence_paths_json,created_at,created_by) VALUES ('i1',?,'r2','a','valid','x',"
                       "'[{\"type\":\"file\",\"path\":\"f.py\",\"note\":\"n\"}]','[\"f.py\"]',?,'t')", (SNAP, SNAP))
        self.c.execute("INSERT INTO dispatch_ticket(run_id,issue_id,identifier,snapshot_updated_at,verdict_id) "
                       "VALUES ('d1','i1','T',?,1)", (SNAP,))
        self.c.execute("UPDATE dispatch SET repos_json='[{\"repo\":\"r\",\"trunk_sha\":\"a\"},"
                       "{\"repo\":\"r2\",\"trunk_sha\":\"a\"}]' WHERE run_id='d1'")
        self.seed_rule("Never ship on a Friday", scope="r")
        seen = {}

        def stub(cfg, state, questions, timeout=None):
            seen.update(state=state, questions=questions)
            return ok_result("human", 0.9)(cfg, state, questions)

        did = self.plan_decision(node_id="T")
        self.assertEqual(jev.repos_for(self.c, decide.one(self.c, did)), {"r2"})
        step = self.plan_decision(node_id="T/1.2")  # a step question narrows to its ticket too
        self.assertEqual(jev.repos_for(self.c, decide.one(self.c, step)), {"r2"})
        with mock.patch("factory.jev.evaluate", side_effect=stub):
            jev.refresh(self.cfg, self.c)
        self.assertNotIn("rules", seen["state"])  # the r-rule was never offered to a r2 question
        self.assertEqual([k for k in seen["questions"] if k.startswith("rule_")], [])  # no rule question either
        self.assertNotIn("rule", decide.one(self.c, did)["jev"])


class Focus(unittest.TestCase):
    def test_focus_criteria_are_the_present_differing_consequences(self):
        opts = [{"id": "a", "label": "A", "leads_to": "x", "changes": [], "result": "r", "cost": "5 min",
                 "risk": "none"},
                {"id": "b", "label": "B", "leads_to": "y", "changes": [], "result": "r", "cost": "5 min",
                 "risk": "full rescan"}]
        self.assertEqual(jev._focus_criteria({"kind": "plan", "options": opts}), ["risk"])
        opts[1]["risk"] = "none"
        opts[0]["changes"] = [{"step": "FIN-1/1", "becomes": "other"}]
        self.assertEqual(jev._focus_criteria({"kind": "plan", "options": opts}), ["changes"])
        opts[1]["changes"] = opts[0]["changes"]
        self.assertEqual(jev._focus_criteria({"kind": "plan", "options": opts}), [])
        # executor (ask) options carry their consequence only as leads_to: that is the result to highlight
        legacy = [{"id": "a", "label": "A", "leads_to": "x"}, {"id": "b", "label": "B", "leads_to": "y"}]
        self.assertEqual(jev._focus_criteria({"kind": "ask", "options": legacy}), ["result"])
        same = [{"id": "a", "label": "A", "leads_to": "x"}, {"id": "b", "label": "B", "leads_to": "x"}]
        self.assertEqual(jev._focus_criteria({"kind": "ask", "options": same}), [])

    def test_focus_highlights_the_differing_consequence(self):
        c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        cfg = SimpleNamespace(raw={"jev": {"enabled": True}})
        c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
                  "VALUES ('d1','draft','[]','t',?)", (SNAP,))
        did = decide.open_(c, "plan", "q?", [{**decide.option("a", "A", "x"), "cost": "5 minutes"},
                                             {**decide.option("b", "B", "y"), "cost": "a full day"}],
                           "a", "why", "t", run_id="d1")
        with mock.patch("factory.jev.evaluate", side_effect=ok_result("human", 0.9, focus="cost")):
            jev.refresh(cfg, c)
        self.assertEqual(decide.one(c, did)["jev"]["focus"], "cost")


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
        with mock.patch("factory.jev.evaluate", side_effect=ok_result("investigate", 0.9)) as ev:
            with self.assertRaises(dispatch.StageError) as ctx:
                dispatch.plan(self.cfg, self.c, "d1", self.payload())
        self.assertIn("'flag'", str(ctx.exception))  # names the question: actionable for the planner
        d = self.c.execute("SELECT state, planned_at FROM dispatch WHERE run_id='d1'").fetchone()
        self.assertEqual((d["state"], d["planned_at"]), ("draft", None))
        self.assertEqual(self.c.execute("SELECT count(*) FROM dispatch_step").fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT count(*) FROM decision").fetchone()[0], 0)
        ev.assert_called_once()  # the review is not judged once the gate fires

    def test_an_unsure_investigation_commits_with_guidance(self):
        with mock.patch("factory.jev.evaluate", side_effect=ok_result("investigate", 0.7)):
            res = dispatch.plan(self.cfg, self.c, "d1", self.payload())
        self.assertEqual(res["questions"], 1)
        q = next(d for d in decide.rows(self.c, "d1") if d["kind"] == "plan")
        self.assertEqual(q["jev"]["category"], "investigate")
        self.assertEqual(q["jev"]["confidence"], 0.7)

    def test_plan_commits_with_guidance_when_jev_succeeds(self):
        with mock.patch("factory.jev.evaluate", side_effect=ok_result("human", 0.9)):
            res = dispatch.plan(self.cfg, self.c, "d1", self.payload())
        self.assertEqual(res["recommend"], "approve")
        by_kind = {d["kind"]: d for d in decide.rows(self.c, "d1")}
        self.assertEqual(by_kind["plan"]["jev"]["status"], "ok")
        self.assertEqual(by_kind["review"]["jev"]["status"], "ok")
        for k in ("plan", "review"):  # advice for the new decision ids, in jev_advice, never in detail_json
            self.assertEqual(jev.stored(self.c, by_kind[k]["id"])["status"], "ok")
            self.assertNotIn("jev", by_kind[k]["detail"])

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
        # _deadline itself handles the raw row (tier='auto', due_at set): no reliance on _row's normalization
        raw = self.c.execute("SELECT * FROM decision WHERE id=?", (legacy,)).fetchone()
        self.assertIsNone(decide._deadline(self.c, dict(raw))[0])
        self.assertEqual(decide.sweep(self.cfg, self.c), [])
        self.assertTrue(decide.one(self.c, legacy)["open"])


class Storage(unittest.TestCase):
    """jev_advice helpers: upsert only while open, never into detail_json; decision_answer_once is untouched."""
    def setUp(self):
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        self.cfg = SimpleNamespace(raw={})
        self.next_run = 0

    def open_plan(self):
        self.next_run += 1
        rid = f"d{self.next_run}"  # one dispatch per decision: a run_id is unique
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
                       "VALUES (?,?,?,?,?)", (rid, "draft", "[]", "t", SNAP))
        return decide.open_(self.c, "plan", "q?", [decide.option("a", "A", "x"), decide.option("b", "B", "y")],
                            "a", "why", "t", run_id=rid)

    def test_store_upserts_while_open_and_stored_reads_it_back(self):
        did = self.open_plan()
        advice = {"status": "ok", "model": "jev-1.13.0", "fingerprint": "f1", "assessed_at": SNAP}
        self.assertTrue(jev.store(self.c, did, advice))
        self.assertEqual(jev.stored(self.c, did), advice)
        advice["model"] = "jev-2.0.0"
        self.assertTrue(jev.store(self.c, did, advice))  # upsert, never a second row
        self.assertEqual(self.c.execute("SELECT count(*) FROM jev_advice WHERE decision_id=?", (did,)).fetchone()[0],
                         1)
        self.assertEqual(jev.stored(self.c, did)["model"], "jev-2.0.0")

    def test_store_refuses_after_an_answer_or_a_void(self):
        did = self.open_plan()
        decide.choose(self.cfg, self.c, did, "a", "user")
        self.assertFalse(jev.store(self.c, did, {"status": "ok"}))
        self.assertIsNone(jev.stored(self.c, did))
        did2 = self.open_plan()
        decide.void(self.c, "id=?", (did2,), "gone")
        self.assertFalse(jev.store(self.c, did2, {"status": "ok"}))

    def test_stored_is_none_without_advice_or_a_decision(self):
        self.assertIsNone(jev.stored(self.c, 12345))
        did = self.open_plan()
        self.assertIsNone(jev.stored(self.c, did))

    def test_read_returns_the_payload_with_stale_claims_dropped(self):
        did = self.open_plan()
        advice = {"status": "ok", "model": "jev-1.13.0", "fingerprint": "f1", "assessed_at": SNAP,
                  "category": "policy", "confidence": 0.9,
                  "rule": {"id": 42, "body": "b", "option_id": "a"}}
        self.assertTrue(jev.store(self.c, did, advice))
        d = decide.one(self.c, did)
        self.assertNotIn("rule", d["jev"])  # rule 42 does not exist: never shown
        self.assertEqual(d["jev"]["category"], "unclear")  # and its policy claim left with it

    def test_an_open_decision_row_is_still_immutable(self):
        # decision_answer_once, exactly as before v19: no column of an open decision may change, detail_json
        # included — guidance goes to jev_advice instead.
        did = self.open_plan()
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("UPDATE decision SET detail_json='{\"jev\":{\"status\":\"ok\"}}' WHERE id=?", (did,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("UPDATE decision SET tier='now' WHERE id=?", (did,))


if __name__ == "__main__":
    unittest.main()
