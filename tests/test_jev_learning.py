"""Jev guidance on learnings: proposed learnings are compared through core's factory.jev against a bounded
same-repo candidate shortlist and each judgment is persisted through the real jev.store/jev.stored pair (the
jev_advice row, never decision.detail_json — the immutable decision_answer_once trigger stays untouched), while
plan step text is no longer harvested as a code fact at all. Only jev.evaluate, the model boundary, is patched;
the real persistence path and DB triggers run unchanged."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import db, decide, jev, learn, repos

SNAP = "2026-09-01T00:00:00Z"


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
        self.dbpath = Path(tempfile.mkdtemp()) / "t.db"
        self.c = db.connect(self.dbpath)
        self.cfg = SimpleNamespace(mirror_path=lambda _: self.repo, raw={})
        self.n = 0
        self.c.execute("INSERT OR REPLACE INTO repo_trunk VALUES ('r','main',?,?)", (self.sha, SNAP))
        self.c.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1',?,?,'unstarted',1,'{}')", (SNAP, SNAP))

    def verdict(self, note=""):
        ev = [{"type": "file", "path": "other.py", "note": note, "sha": self.sha}]
        self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,repo,trunk_sha,kind,reason,evidence_json,"
                       "evidence_paths_json,created_at,created_by) VALUES ('i1',?,'r',?,'valid','x',?,?,?,'t')",
                       (SNAP, self.sha, json.dumps(ev), '["other.py"]', SNAP))
        return self.c.execute("SELECT id FROM verdict ORDER BY id DESC LIMIT 1").fetchone()[0]

    def candidate(self, body, scope="r", kind="codemap", status="active", source=None, anchors=None):
        # Distinct source AND anchors per call: the learning dedupe key is (kind, scope, source, anchors), so a
        # shared fixture default would collapse every candidate into one row.
        self.n += 1
        self.c.execute("INSERT INTO learning(kind,scope,body,anchors_json,trunk_sha,source,status,created_at) "
                       "VALUES (?,?,?,?,NULL,?,?,?)",
                       (kind, scope, body, json.dumps(anchors if anchors is not None else [f"cand{self.n}.py"]),
                        source if source is not None else f"verdict:{self.n}", status, SNAP))
        return self.c.execute("SELECT id FROM learning ORDER BY id DESC LIMIT 1").fetchone()[0]

    def propose(self, body, scope="r", kind="pitfall", source="decision:9"):
        learn._propose(self.c, kind, scope, body, [], None, source, "a block cost a dispatch run")
        lid = self.c.execute("SELECT id FROM learning WHERE body=?", (body,)).fetchone()[0]
        did = self.c.execute("SELECT id FROM decision WHERE kind='learning' AND ref=?",
                             (str(lid),)).fetchone()[0]
        return lid, did

    def advice(self, did):
        advice = jev.stored(self.c, did)
        detail = json.loads(self.c.execute("SELECT detail_json FROM decision WHERE id=?",
                                           (did,)).fetchone()[0])
        self.assertNotIn("jev", detail)  # guidance never touches decision detail_json
        return advice

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
        with mock.patch.object(jev, "evaluate", side_effect=lambda cfg, state, questions: ok(f"duplicate:{cid}")) as ev:
            learn.sync(self.cfg, self.c)
            learn.sync(self.cfg, self.c)
            self.assertEqual(ev.call_count, 1)  # unchanged successful judgment reused
        j = self.advice(did)
        self.assertEqual(j["status"], "ok")
        self.assertEqual(j["relation"], {"kind": "duplicate", "learning_id": cid,
                                         "body": "other.py: x lives here"})
        self.assertEqual(j["group"], f"learning:{cid}")
        self.assertIn("assessed_at", j)
        self.assertIn("fingerprint", j)
        self.assertEqual(self.c.execute("SELECT status FROM learning WHERE id=?",
                                        (lid,)).fetchone()[0], "proposed")  # guidance never alters status

    def test_candidate_status_or_body_change_reassesses(self):
        cid = self.candidate("other.py: x lives here")
        self.propose("the parser lives in other.py")
        with mock.patch.object(jev, "evaluate", side_effect=lambda cfg, state, questions: ok(f"duplicate:{cid}")) as ev:
            learn.sync(self.cfg, self.c)
            self.c.execute("UPDATE learning SET body=? WHERE id=?", ("other.py: y lives here", cid))
            learn.sync(self.cfg, self.c)
            self.c.execute("UPDATE learning SET status='proposed' WHERE id=?", (cid,))
            learn.sync(self.cfg, self.c)
            self.assertEqual(ev.call_count, 3)

    def test_unavailable_is_visible_and_retried_next_tick(self):
        self.candidate("other.py: x lives here")
        _, did = self.propose("the parser lives in other.py")
        with mock.patch.object(jev, "evaluate",
                               side_effect=lambda cfg, state, questions: {"status": "unavailable"}) as ev:
            learn.sync(self.cfg, self.c)
            self.assertEqual(self.advice(did)["status"], "unavailable")
            learn.sync(self.cfg, self.c)
            self.assertEqual(ev.call_count, 2)  # failed calls retried, no loop beyond the next tick

    def test_disabled_is_visible_without_crashing_on_an_unconfigured_cfg(self):
        self.candidate("other.py: x lives here")
        _, did = self.propose("the parser lives in other.py")
        with mock.patch.object(jev, "evaluate",
                               side_effect=lambda cfg, state, questions: {"status": "disabled"}):
            learn.sync(SimpleNamespace(raw={}), self.c)
        self.assertEqual(self.advice(did)["status"], "disabled")

    def test_relation_to_an_unknown_candidate_is_not_trusted(self):
        self.candidate("other.py: x lives here")
        _, did = self.propose("the parser lives in other.py")
        with mock.patch.object(jev, "evaluate", side_effect=lambda cfg, state, questions: ok("duplicate:999")):
            learn.sync(self.cfg, self.c)
        j = self.advice(did)
        self.assertEqual(j["status"], "ok")
        self.assertNotIn("relation", j)

    def test_low_confidence_relationship_is_recorded_but_never_surfaced_as_definitive(self):
        cid = self.candidate("other.py: x lives here")
        _, did = self.propose("the parser lives in other.py")
        with mock.patch.object(jev, "evaluate",
                               side_effect=lambda cfg, state, questions: ok(f"duplicate:{cid}", confidence=0.5)):
            learn.sync(self.cfg, self.c)
        j = self.advice(did)
        self.assertEqual(j["status"], "ok")
        self.assertNotIn("relation", j)
        self.assertNotIn("group", j)

    def test_conflicts_is_surfaced_without_a_group(self):
        cid = self.candidate("other.py: x lives here")
        _, did = self.propose("the parser lives in other.py")
        with mock.patch.object(jev, "evaluate", side_effect=lambda cfg, state, questions: ok(f"conflicts:{cid}")):
            learn.sync(self.cfg, self.c)
        j = self.advice(did)
        self.assertEqual(j["relation"], {"kind": "conflicts", "learning_id": cid,
                                         "body": "other.py: x lives here"})
        self.assertNotIn("group", j)

    def test_same_tick_group_chain_adopts_the_root_stored_earlier(self):
        c0 = self.candidate("a.py: root", source="verdict:0")
        lid1, did1 = self.propose("b.py: middle again", kind="pitfall", source="decision:1")
        lid2, did2 = self.propose("c.py: middle again", kind="pitfall", source="decision:2")

        def respond(cfg, state, questions):
            body = state["proposal"]["body"]
            return ok(f"duplicate:{c0 if body.startswith('b.py') else lid1}")

        with mock.patch.object(jev, "evaluate", side_effect=respond):
            learn.sync(self.cfg, self.c)  # one tick: the second proposal sees the first's stored group root
        self.assertEqual(self.advice(did1)["group"], f"learning:{c0}")
        g2 = self.advice(did2)["group"]
        self.assertEqual(g2, f"learning:{c0}")  # adopts the component root, not the direct target
        self.assertLess(int(g2.split(":")[1]), lid2)  # group targets always sit below the member's own id

    def test_unrelated_repo_candidate_is_never_compared(self):
        other = self.candidate("other.py: x lives here", scope="elsewhere")
        _, did = self.propose("the parser lives in other.py")
        with mock.patch.object(jev, "evaluate",
                               side_effect=lambda cfg, state, questions: ok(f"duplicate:{other}")) as ev:
            learn.sync(self.cfg, self.c)
            self.assertEqual(ev.call_count, 0)  # different repo: never compared
        self.assertNotIn("relation", self.advice(did) or {})

    def test_a_vanished_candidate_clears_the_stale_relationship(self):
        cid = self.candidate("other.py: x lives here")
        _, did = self.propose("the parser lives in other.py")
        with mock.patch.object(jev, "evaluate", side_effect=lambda cfg, state, questions: ok(f"duplicate:{cid}")):
            learn.sync(self.cfg, self.c)
        self.assertIn("relation", self.advice(did))
        self.c.execute("UPDATE learning SET status='expired' WHERE id=?", (cid,))
        with mock.patch.object(jev, "evaluate") as ev:
            learn.sync(self.cfg, self.c)
            self.assertEqual(ev.call_count, 0)  # nothing left to compare
        j = self.advice(did)
        self.assertNotIn("relation", j)
        self.assertNotIn("group", j)

    def test_rows_expose_guidance_on_the_learn_surface(self):
        cid = self.candidate("other.py: x lives here")
        lid, _ = self.propose("the parser lives in other.py")
        with mock.patch.object(jev, "evaluate", side_effect=lambda cfg, state, questions: ok(f"duplicate:{cid}")):
            learn.sync(self.cfg, self.c)
        row = next(r for r in learn.rows(self.c) if r["id"] == lid)
        self.assertEqual(row["jev"]["relation"], {"kind": "duplicate", "learning_id": cid,
                                                  "body": "other.py: x lives here"})

    def test_rows_never_show_a_stale_relation_or_group_once_the_target_rejects_or_expires(self):
        cid = self.candidate("other.py: x lives here")
        lid, did = self.propose("the parser lives in other.py")
        with mock.patch.object(jev, "evaluate", side_effect=lambda cfg, state, questions: ok(f"duplicate:{cid}")):
            learn.sync(self.cfg, self.c)
        self.assertIn("relation", jev.stored(self.c, did))  # raw storage keeps the record
        for status in ("rejected", "expired"):
            self.c.execute("UPDATE learning SET status=? WHERE id=?", (status, cid))
            with mock.patch.object(jev, "evaluate",
                                   side_effect=AssertionError("read paths never touch the model")) as ev:
                row = next(r for r in learn.rows(self.c) if r["id"] == lid)
                self.assertEqual(ev.call_count, 0)  # real storage: no network on reads
            self.assertIn("jev", row)
            self.assertNotIn("relation", row["jev"])
            self.assertNotIn("group", row["jev"])
            self.assertIn("relation", jev.stored(self.c, did))  # display filtering, not storage mutation
            self.c.execute("UPDATE learning SET status='active' WHERE id=?", (cid,))

    def test_a_decision_answered_while_the_call_is_in_flight_is_left_untouched(self):
        cid = self.candidate("other.py: x lives here")
        lid, did = self.propose("the parser lives in other.py")

        def answer_then_ok(cfg, state, questions):
            decide.choose(self.cfg, self.c, did, "reject", "user")
            return ok(f"duplicate:{cid}")

        with mock.patch.object(jev, "evaluate", side_effect=answer_then_ok):
            learn.sync(self.cfg, self.c)
        self.assertIsNone(jev.stored(self.c, did))  # real jev.store recheck: guidance never lands on a closed decision
        self.assertEqual(self.c.execute("SELECT status FROM learning WHERE id=?",
                                        (lid,)).fetchone()[0], "rejected")

    def test_persisted_advice_survives_reopen(self):
        cid = self.candidate("other.py: x lives here")
        _, did = self.propose("the parser lives in other.py")
        with mock.patch.object(jev, "evaluate", side_effect=lambda cfg, state, questions: ok(f"duplicate:{cid}")):
            learn.sync(self.cfg, self.c)
        before = jev.stored(self.c, did)
        self.c.close()
        c2 = db.connect(self.dbpath)
        try:
            self.assertEqual(jev.stored(c2, did), before)  # a real jev_advice row, not an in-memory double
        finally:
            c2.close()

    def test_shortlist_is_bounded_same_kind_first_and_same_repo_only(self):
        for i in range(50):
            self.candidate(f"c{i}.py: codemap fact")
        for i in range(5):
            self.candidate(f"p{i}.py: pitfall fact", kind="pitfall")
        elsewhere = self.candidate("z.py: elsewhere fact", scope="elsewhere", kind="pitfall")
        self.propose("q.py: new pitfall", kind="pitfall")
        seen = []
        with mock.patch.object(jev, "evaluate",
                               side_effect=lambda cfg, state, questions: seen.append(state) or ok("none")):
            learn.sync(self.cfg, self.c)
        cands = seen[0]["candidates"]
        self.assertEqual(len(cands), 40)  # capped well inside the 255-choice ceiling
        self.assertTrue(all(c["kind"] == "pitfall" for c in cands[:5]))  # same kind leads
        self.assertNotIn(elsewhere, [c["id"] for c in cands])  # same repo is mandatory

    def test_only_older_proposed_candidates_join_but_active_ones_of_any_age_do(self):
        lid, _ = self.propose("q.py: new pitfall", kind="pitfall")
        newer_proposed = self.candidate("n.py: later proposal", kind="pitfall", status="proposed")
        newer_active = self.candidate("m.py: later kept", kind="pitfall", status="active")
        self.assertGreater(newer_proposed, lid)
        self.assertGreater(newer_active, lid)
        seen = []
        with mock.patch.object(jev, "evaluate",
                               side_effect=lambda cfg, state, questions: seen.append(state) or ok("none")):
            learn.sync(self.cfg, self.c)
        ids = [c["id"] for c in seen[0]["candidates"]]
        self.assertNotIn(newer_proposed, ids)  # no forward pointers to younger proposals
        self.assertIn(newer_active, ids)


if __name__ == "__main__":
    unittest.main()
