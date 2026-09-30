"""Jev guidance on learnings: proposed learnings are compared (via core's factory.jev) with bounded same-scope
candidates, the judgment lands on the open learning decision's detail_json['jev'], unchanged successes are reused
and failed/disabled calls stay visible — while plan step text is no longer harvested as a code fact at all.

INTEGRATION NOTE: persisting guidance needs `decision.detail_json` to be updatable while a decision is open. The
v18 `decision_answer_once` trigger aborts that update (it only lets a decision be answered or withdrawn). The
parent's schema migration must allow detail_json changes on open decisions while keeping answered/voided ones
frozen, e.g. replace the trigger's identity-change list so detail_json is no longer in it and amend the
`(NEW.chosen IS NULL AND NEW.void_reason IS NULL)` clause to `... AND NEW.detail_json IS OLD.detail_json`."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from factory import db, decide, learn, repos

SNAP = "2026-09-01T00:00:00Z"


class FakeJev:
    def __init__(self, respond):
        self.respond = respond  # callable(cfg, state, questions) -> dict
        self.calls = 0

    def evaluate(self, cfg, state, questions):
        self.calls += 1
        return self.respond(cfg, state, questions)


def ok(choice, confidence=0.9):
    return {"status": "ok", "model": "jev-1.13.0",
            "answers": {"relation": {"type": "choice", "choice": choice, "confidence": confidence}},
            "usage": {"input_tokens": 10, "output_tokens": 5}}


class JevLearning(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.git = lambda *a: repos.git(self.repo, *a)
        self.git("init", "-q")
        self.git("config", "user.email", "t@x"); self.git("config", "user.name", "t")
        (self.repo / "cited.py").write_text("x = 1\n")
        self.git("add", "."); self.git("commit", "-qm", "one")
        self.sha = self.git("rev-parse", "HEAD")
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        self.cfg = SimpleNamespace(mirror_path=lambda _: self.repo, raw={})
        self.c.execute("INSERT OR REPLACE INTO repo_trunk VALUES ('r','main',?,?)", (self.sha, SNAP))
        self.c.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1',?,?,'unstarted',1,'{}')", (SNAP, SNAP))
        self.orig_jev = learn.jev

    def tearDown(self):
        learn.jev = self.orig_jev

    def verdict(self, note=""):
        ev = [{"type": "file", "path": "other.py", "note": note, "sha": self.sha}]
        self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,repo,trunk_sha,kind,reason,evidence_json,"
                       "evidence_paths_json,created_at,created_by) VALUES ('i1',?,'r',?,'valid','x',?,?,?,'t')",
                       (SNAP, self.sha, json.dumps(ev), '["other.py"]', SNAP))
        return self.c.execute("SELECT id FROM verdict ORDER BY id DESC LIMIT 1").fetchone()[0]

    def candidate(self, body, scope="r", status="active", source="verdict:1"):
        self.c.execute("INSERT INTO learning(kind,scope,body,anchors_json,trunk_sha,source,status,created_at) "
                       "VALUES ('codemap',?,?,'[\"other.py\"]',NULL,?,?,?)", (scope, body, source, status, SNAP))
        return self.c.execute("SELECT id FROM learning ORDER BY id DESC LIMIT 1").fetchone()[0]

    def propose(self, body, scope="r", kind="pitfall", source="decision:9"):
        learn._propose(self.c, kind, scope, body, [], None, source, "a block cost a dispatch run")
        lid = self.c.execute("SELECT id FROM learning WHERE body=?", (body,)).fetchone()[0]
        did = self.c.execute("SELECT id FROM decision WHERE kind='learning' AND ref=?",
                             (str(lid),)).fetchone()[0]
        return lid, did

    def jev(self, did):
        return json.loads(self.c.execute("SELECT detail_json FROM decision WHERE id=?",
                                         (did,)).fetchone()[0]).get("jev")

    # ---- plan step text is no longer a code fact ---------------------------------------------------------------
    def test_plan_step_text_is_not_harvested_as_a_code_map_fact(self):
        vid = self.verdict()
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
                       "VALUES ('d2','draft',?,'x',?)", (json.dumps([{"repo": "r", "trunk_sha": self.sha}]), SNAP))
        self.c.execute("INSERT INTO dispatch_ticket(run_id,issue_id,identifier,snapshot_updated_at,verdict_id) "
                       "VALUES ('d2','i1','FIN-1',?,?)", (SNAP, vid))
        self.c.execute("INSERT INTO dispatch_step(run_id,step_id,parent,title,detail,depends_on_json,result,"
                       "files_json) VALUES ('d2','FIN-1/1',NULL,'fix cited.py parser','the parser lives in "
                       "cited.py','[]',NULL,NULL)")
        learn.sync(self.cfg, self.c)
        got = [r["body"] for r in self.c.execute("SELECT body FROM learning WHERE source LIKE 'step:%'")]
        self.assertEqual(got, [])

    def test_existing_step_sourced_codemap_is_expired_with_reason_and_kept_as_audit(self):
        self.c.execute("INSERT INTO learning(kind,scope,body,anchors_json,trunk_sha,source,status,created_at) "
                       "VALUES ('codemap','r','cited.py: the parser lives here','[\"cited.py\"]',?,"
                       "'step:d2:FIN-1/1','active',?)", (self.sha, SNAP))
        learn.sync(self.cfg, self.c)
        row = self.c.execute("SELECT status, expired_reason, body FROM learning "
                             "WHERE source='step:d2:FIN-1/1'").fetchone()
        self.assertEqual(row["status"], "expired")
        self.assertIn("plan step text is no longer a code fact source", row["expired_reason"])
        self.assertEqual(row["body"], "cited.py: the parser lives here")  # audit preserved

    # ---- relationship guidance ----------------------------------------------------------------------------------
    def test_duplicate_guidance_lands_on_the_open_decision_and_reuses_unchanged_inputs(self):
        cid = self.candidate("other.py: x lives here")
        lid, did = self.propose("the parser lives in other.py")
        learn.jev = FakeJev(lambda cfg, state, questions: ok(f"duplicate:{cid}"))
        learn.sync(self.cfg, self.c)
        j = self.jev(did)
        self.assertEqual(j["status"], "ok")
        self.assertEqual(j["model"], "jev-1.13.0")
        self.assertEqual(j["relation"], {"kind": "duplicate", "learning_id": cid,
                                         "body": "other.py: x lives here"})
        self.assertEqual(j["group"], f"learning:{cid}")
        self.assertIn("assessed_at", j)
        self.assertIn("fingerprint", j)
        self.assertEqual(self.c.execute("SELECT status FROM learning WHERE id=?",
                                        (lid,)).fetchone()[0], "proposed")  # guidance never alters status
        learn.sync(self.cfg, self.c)
        self.assertEqual(learn.jev.calls, 1)  # unchanged successful judgment reused
        self.assertEqual(self.jev(did), j)

    def test_candidate_status_or_body_change_reassesses(self):
        cid = self.candidate("other.py: x lives here")
        _, did = self.propose("the parser lives in other.py")
        learn.jev = FakeJev(lambda cfg, state, questions: ok(f"duplicate:{cid}"))
        learn.sync(self.cfg, self.c)
        self.c.execute("UPDATE learning SET body=? WHERE id=?", ("other.py: y lives here", cid))
        learn.sync(self.cfg, self.c)
        self.assertEqual(learn.jev.calls, 2)
        self.c.execute("UPDATE learning SET status='proposed' WHERE id=?", (cid,))
        learn.sync(self.cfg, self.c)
        self.assertEqual(learn.jev.calls, 3)

    def test_unavailable_is_visible_and_retried_next_tick(self):
        self.candidate("other.py: x lives here")
        _, did = self.propose("the parser lives in other.py")
        learn.jev = FakeJev(lambda cfg, state, questions: {"status": "unavailable", "error": "timeout"})
        learn.sync(self.cfg, self.c)
        j = self.jev(did)
        self.assertEqual(j["status"], "unavailable")
        self.assertEqual(j["error"], "timeout")
        learn.sync(self.cfg, self.c)
        self.assertEqual(learn.jev.calls, 2)  # failed calls retried, no loop beyond the next tick

    def test_disabled_is_visible_without_crashing_on_an_unconfigured_cfg(self):
        self.candidate("other.py: x lives here")
        _, did = self.propose("the parser lives in other.py")
        learn.jev = FakeJev(lambda cfg, state, questions: {"status": "disabled", "error": "no key"})
        learn.sync(SimpleNamespace(raw={}), self.c)
        j = self.jev(did)
        self.assertEqual(j["status"], "disabled")
        self.assertEqual(j["error"], "no key")

    def test_relation_to_an_unknown_candidate_is_not_trusted(self):
        self.candidate("other.py: x lives here")
        _, did = self.propose("the parser lives in other.py")
        learn.jev = FakeJev(lambda cfg, state, questions: ok("duplicate:999"))
        learn.sync(self.cfg, self.c)
        j = self.jev(did)
        self.assertEqual(j["status"], "ok")
        self.assertNotIn("relation", j)

    def test_conflicts_is_surfaced_without_a_group(self):
        cid = self.candidate("other.py: x lives here")
        _, did = self.propose("the parser lives in other.py")
        learn.jev = FakeJev(lambda cfg, state, questions: ok(f"conflicts:{cid}"))
        learn.sync(self.cfg, self.c)
        j = self.jev(did)
        self.assertEqual(j["relation"], {"kind": "conflicts", "learning_id": cid,
                                         "body": "other.py: x lives here"})
        self.assertNotIn("group", j)

    def test_a_new_duplicate_adopts_the_targets_group_and_links_never_cycle(self):
        c0 = self.candidate("a.py: root", source="verdict:0")
        lid1, did1 = self.propose("b.py: middle again", kind="pitfall", source="decision:1")

        def respond(cfg, state, questions):
            return ok(f"duplicate:{c0 if state['proposal']['body'].startswith('b.py') else lid1}")

        learn.jev = FakeJev(respond)
        learn.sync(self.cfg, self.c)
        self.assertEqual(self.jev(did1)["group"], f"learning:{c0}")
        lid2, did2 = self.propose("c.py: middle again", kind="pitfall", source="decision:2")
        learn.sync(self.cfg, self.c)
        g2 = self.jev(did2)["group"]
        self.assertEqual(g2, f"learning:{c0}")  # adopts the component root, not the direct target
        self.assertLess(int(g2.split(":")[1]), lid2)  # group targets always sit below the member's own id

    def test_unrelated_repo_and_no_candidates_are_not_compared(self):
        self.candidate("other.py: x lives here", scope="elsewhere")
        _, did = self.propose("the parser lives in other.py")
        learn.jev = FakeJev(lambda cfg, state, questions: ok("none"))
        learn.sync(self.cfg, self.c)
        self.assertEqual(learn.jev.calls, 0)
        self.assertIsNone(self.jev(did))

    def test_rows_expose_guidance_on_the_learn_surface(self):
        cid = self.candidate("other.py: x lives here")
        lid, _ = self.propose("the parser lives in other.py")
        learn.jev = FakeJev(lambda cfg, state, questions: ok(f"duplicate:{cid}"))
        learn.sync(self.cfg, self.c)
        row = next(r for r in learn.rows(self.c) if r["id"] == lid)
        self.assertEqual(row["jev"]["relation"], {"kind": "duplicate", "learning_id": cid,
                                                  "body": "other.py: x lives here"})

    def test_a_decision_answered_while_the_call_is_in_flight_is_left_untouched(self):
        self.candidate("other.py: x lives here")
        lid, did = self.propose("the parser lives in other.py")

        def answer_then_ok(cfg, state, questions):
            decide.choose(self.cfg, self.c, did, "reject", "user")
            return ok(f"duplicate:{lid - 1}")

        learn.jev = FakeJev(answer_then_ok)
        learn.sync(self.cfg, self.c)
        detail = json.loads(self.c.execute("SELECT detail_json FROM decision WHERE id=?",
                                           (did,)).fetchone()[0])
        self.assertNotIn("jev", detail)  # guidance never rewrites a closed decision
        self.assertEqual(self.c.execute("SELECT status FROM learning WHERE id=?",
                                        (lid,)).fetchone()[0], "rejected")


if __name__ == "__main__":
    unittest.main()
