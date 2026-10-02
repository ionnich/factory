"""Domain grooming: durable real-model review, strict output validation, human approval freeze, reconcile apply."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factory import db, domain_groom, reconcile
from factory.config import Config, Context
from factory.dispatch import StageError

SNAP = "2026-09-01T00:00:00.000Z"
SNAP2 = "2026-09-02T00:00:00.000Z"
LEAD = "me@example.com"
TEAM = {"TEAM": {"review_state": "In Review", "done_state": "Done",
                 "canceled_state": "Canceled", "todo_state": "Todo"}}

DOMAIN_BODY = "Domain: My Domain\n"


def raw(ident, state="Todo", assignee=None, state_type="unstarted", description=DOMAIN_BODY):
    stype = "completed" if state == "Done" else "canceled" if state == "Canceled" else state_type
    return stype, json.dumps({"identifier": ident, "title": f"Work {ident}", "url": f"https://linear/{ident}",
                              "description": description, "state": {"name": state, "type": stype},
                              "assignee": {"email": assignee} if assignee else None,
                              "priority": 3, "labels": {"nodes": []}, "team": {"key": "TEAM"},
                              "project": {"name": "My Domain"}})


def body(title="T", outcome="do it"):
    return {"title": title, "outcome": outcome, "acceptance": ["a"], "scope": ["s"], "exclusions": [],
            "decisions": [], "dependencies": [], "resources": [], "risks": [], "evidence": []}


def disposition(ident, action, **over):
    d = {"identifier": ident, "action": action, "reason": f"reason {ident}", "evidence": ["recorded snapshot"],
         "cut_ids": [], "title": None, "description": None, "target": None}
    d.update(over)
    return d


def result(tickets, **over):
    r = {"minimum_system": "a minimum", "consumers": ["consumer"], "correctness": ["invariant"],
         "cuts": [], "tickets": tickets, "simplification": None, "limitations": ["recorded facts only"]}
    r.update(over)
    return r


class Grooming(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = db.connect(Path(self.tmp.name) / "f.db")
        self.addCleanup(self.c.close)
        self.cfg = Config(raw={"linear": {"lead": LEAD, "team": TEAM}},
                          db=Path(self.tmp.name) / "f.db", mirrors=Path(self.tmp.name) / "m",
                          dispatches=Path(self.tmp.name) / "d",
                          contexts=[Context(name="ctx", repo="Finks-ai/finks-ddd", domains=["My Domain"],
                                            route="fx-news")],
                          repos={}, witnesses={})
        self.c.execute("INSERT INTO linear_project(id,slug_id,name,lead_email,fetched_at) VALUES "
                       "('p1','my-domain','My Domain',?,?)", (LEAD, SNAP))
        self.c.execute("INSERT INTO repo_trunk VALUES ('Finks-ai/finks-ddd','main','sha1',?)", (SNAP,))

    def insert(self, ident, state="Todo", assignee=None, state_type="unstarted", updated_at=SNAP):
        stype, r = raw(ident, state, assignee, state_type)
        self.c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,?,1,?)",
                       (ident.lower(), ident, updated_at, SNAP, stype, r))
        self.c.execute("INSERT OR REPLACE INTO linear_relationship(identifier, observed_at, edges_json, nodes_json, "
                       "fingerprint) VALUES (?,?,?,?,?)", (ident, SNAP, "[]", "[]", "fp"))

    def complete(self, domain_id="p1", tickets=None, simplification=None, mutable=True):
        ctx = {"domain": {"id": domain_id, "name": "My Domain", "slug_id": "my-domain"}, "goal": "",
               "tickets": [{"identifier": t, "issue_id": t.lower(), "title": f"Work {t}",
                            "description": DOMAIN_BODY, "url": f"https://linear/{t}", "state": "Todo",
                            "state_type": "unstarted", "assignee": None, "team_key": "TEAM",
                            "repo": "Finks-ai/finks-ddd", "context": "ctx", "route": "fx-news",
                            "verdict_kind": None, "verdict_reason": None, "evidence": [],
                            "relationships": {"complete": True, "observed_at": SNAP, "edges": [], "nodes": [],
                                             "fingerprint": "fp"},
                            "snapshot_updated_at": SNAP, "snapshot_fetched_at": SNAP,
                            "priority": 3, "created_at": None, "mutable": mutable, "blocker": None} for t in tickets],
               "completed_sources": [], "briefs": [],
               "mirrors": [{"repo": "Finks-ai/finks-ddd", "path": "/m", "branch": "main", "trunk_sha": "sha1",
                            "fetched_at": SNAP}]}
        res = result([disposition(t, "keep") if mutable is False else disposition(t, "keep") for t in tickets],
                     simplification=simplification)
        rid = self.c.execute("INSERT INTO domain_review(domain_id, goal, status, requested_at, completed_at, "
                             "result_json, context_json) VALUES ('p1','','completed',?,?,?,?)",
                             (SNAP, SNAP, json.dumps(res), json.dumps(ctx))).lastrowid
        return rid

    def vctx(self, tickets):
        return {"domain": {"id": "p1", "name": "My Domain"}, "tickets": tickets}

    def test_validate_rejects_unknown_fields_and_bad_types(self):
        with self.assertRaises(StageError):
            domain_groom._validate_output({"bogus": 1}, {}, self.c)
        good = result([disposition("FIN-1", "keep")])
        good["tickets"][0]["title"] = "should be null for keep"
        with self.assertRaises(StageError):
            domain_groom._validate_output(good, self.vctx([{"identifier": "FIN-1", "mutable": True,
                                                            "blocker": None}]), self.c)

    def test_validate_requires_complete_open_ticket_coverage(self):
        with self.assertRaises(StageError) as cm:
            domain_groom._validate_output(
                result([disposition("FIN-1", "keep")]),
                self.vctx([{"identifier": "FIN-1", "mutable": True, "blocker": None},
                           {"identifier": "FIN-2", "mutable": True, "blocker": None}]), self.c)
        self.assertIn("FIN-2", str(cm.exception))

    def test_validate_rejects_mutation_of_blocked_ticket(self):
        v = result([disposition("FIN-1", "rewrite", title="New")])
        with self.assertRaises(StageError) as cm:
            domain_groom._validate_output(v, self.vctx([{"identifier": "FIN-1", "mutable": False,
                                                         "blocker": "in a live dispatch"}]), self.c)
        self.assertIn("not mutable", str(cm.exception))

    def test_validate_rewrite_description_must_keep_the_domain_line(self):
        v = result([disposition("FIN-1", "rewrite", title="New", description="No domain line here")])
        with self.assertRaises(StageError) as cm:
            domain_groom._validate_output(v, self.vctx([{"identifier": "FIN-1", "mutable": True,
                                                         "blocker": None}]), self.c)
        self.assertIn("Domain line", str(cm.exception))

    def test_validate_rejects_merge_into_closed_target_and_unknown_target(self):
        tickets = [disposition("FIN-1", "merge", target="FIN-2"),
                   disposition("FIN-2", "merge", target="FIN-1")]
        ctx = self.vctx([{"identifier": "FIN-1", "mutable": True, "blocker": None},
                         {"identifier": "FIN-2", "mutable": True, "blocker": None}])
        with self.assertRaises(StageError):
            domain_groom._validate_output(result(tickets), ctx, self.c)  # cycle: FIN-1 -> FIN-2 -> FIN-1
        bad = [disposition("FIN-1", "merge", target="FIN-9"), disposition("FIN-2", "keep")]
        with self.assertRaises(StageError):
            domain_groom._validate_output(result(bad), ctx, self.c)

    def test_validate_rejects_merge_into_immutable_target(self):
        tickets = [disposition("FIN-1", "merge", target="FIN-2"), disposition("FIN-2", "keep")]
        ctx = self.vctx([{"identifier": "FIN-1", "mutable": True, "blocker": None},
                         {"identifier": "FIN-2", "mutable": False, "blocker": "in a live dispatch"}])
        with self.assertRaises(StageError) as cm:
            domain_groom._validate_output(result(tickets), ctx, self.c)
        self.assertIn("not mutable", str(cm.exception))

    def test_validate_rejects_unknown_cut_and_duplicate_ids(self):
        v = result([disposition("FIN-1", "keep", cut_ids=["nope"])],
                   cuts=[{"id": "c1", "title": "t", "reason": "r", "evidence": [], "risk": "k", "migration": "m"}])
        with self.assertRaises(StageError):
            domain_groom._validate_output(v, self.vctx([{"identifier": "FIN-1", "mutable": True,
                                                         "blocker": None}]), self.c)

    def test_validate_simplification_must_use_retained_sources_and_valid_body(self):
        v = result([disposition("FIN-1", "keep"), disposition("FIN-2", "close")],
                   simplification={"identifiers": ["FIN-2"], "body": body()})
        with self.assertRaises(StageError) as cm:
            domain_groom._validate_output(v, self.vctx([{"identifier": "FIN-1", "mutable": True, "blocker": None},
                                                        {"identifier": "FIN-2", "mutable": True, "blocker": None}]),
                                          self.c)
        self.assertIn("retained", str(cm.exception))

    def test_request_deduplicates_and_spawns_detached(self):
        self.insert("FIN-1")
        spawned = []
        first = domain_groom.request(self.cfg, self.c, "p1", "", spawn=lambda argv, **kw: spawned.append(argv))
        self.assertEqual(first["status"], "pending")
        again = domain_groom.request(self.cfg, self.c, "p1", "", spawn=lambda argv, **kw: spawned.append(argv))
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(spawned), 1)

    def test_request_refuses_foreign_domain_and_bad_goal(self):
        self.c.execute("INSERT INTO linear_project(id,slug_id,name,lead_email,fetched_at) VALUES "
                       "('p2','other','Other','someone@else.com',?)", (SNAP,))
        with self.assertRaises(StageError):
            domain_groom.request(self.cfg, self.c, "p2")
        with self.assertRaises(StageError):
            domain_groom.request(self.cfg, self.c, "p1", "x" * 2001)

    def _run(self, rid, stdout):
        return domain_groom.run(self.cfg, self.c, rid, runner=lambda argv, **kw: SimpleProc(stdout))

    def test_run_commits_valid_result_and_rejects_bad_output(self):
        self.insert("FIN-1")
        rid = domain_groom.request(self.cfg, self.c, "p1", spawn=lambda argv, **kw: None)["id"]
        out = json.dumps({"outcome": "ready", "assessment": "substantiated",
                          "witness_queries": [], "review": result([disposition("FIN-1", "keep")])})
        done = self._run(rid, out)
        self.assertEqual(done["status"], "completed")
        self.assertIsNotNone(done["completed_at"])
        # a bad output fails durably and is retryable
        self.insert("FIN-2")
        rid2 = domain_groom.request(self.cfg, self.c, "p1", spawn=lambda argv, **kw: None)["id"]
        failed = self._run(rid2, '{"minimum_system": "x"}')
        self.assertEqual(failed["status"], "failed")
        self.assertIsNotNone(failed["error"])

    def test_approve_requires_human_completed_and_mutation_only(self):
        self.insert("FIN-1")
        rid = self.complete(tickets=["FIN-1"])
        with self.assertRaises(StageError):
            domain_groom.approve(self.cfg, self.c, rid, ["FIN-1"], "agent:review")
        with self.assertRaises(StageError):  # keep is not a mutation
            domain_groom.approve(self.cfg, self.c, rid, ["FIN-1"], "user:dashboard")
        with self.assertRaises(StageError):  # unknown identifier
            domain_groom.approve(self.cfg, self.c, rid, ["FIN-9"], "user:dashboard")

    def test_approve_queues_writes_and_is_idempotent(self):
        self.insert("FIN-1")
        rid = self.c.execute(
            "INSERT INTO domain_review(domain_id,goal,status,requested_at,completed_at,result_json,context_json) "
            "VALUES ('p1','','completed',?,?,?,?)",
            (SNAP, SNAP, json.dumps(result([disposition("FIN-1", "rewrite", title="New title",
                                                        description="New description\nDomain: My Domain")])),
             json.dumps({"domain": {"id": "p1", "name": "My Domain"}, "goal": "",
                         "tickets": [{"identifier": "FIN-1", "issue_id": "fin-1", "team_key": "TEAM",
                                      "snapshot_updated_at": SNAP, "relationships": {"complete": True,
                                      "fingerprint": "fp"}}],
                         "mirrors": [{"repo": "Finks-ai/finks-ddd", "trunk_sha": "sha1"}]}))).lastrowid
        detail = domain_groom.approve(self.cfg, self.c, rid, ["FIN-1"], "user:dashboard")
        writes = detail["writebacks"]
        self.assertEqual([w["op"] for w in writes], ["description"])
        self.assertTrue(all(w["rule"] == "domain-groom-rewrite" for w in writes))
        self.assertTrue(all(w["decision"] == "apply" and w["status"] == "planned" for w in writes))
        self.assertEqual(writes[0]["payload"]["title"], "New title")
        self.assertEqual(writes[0]["payload"]["description"], "New description\nDomain: My Domain")
        # idempotent: repeat approval does not duplicate writes, even after the ticket changed
        self.insert("FIN-1", updated_at=SNAP2)
        again = domain_groom.approve(self.cfg, self.c, rid, ["FIN-1"], "user:dashboard")
        self.assertEqual(len(again["writebacks"]), len(writes))

    def test_approve_rejects_stale_unapproved_ticket(self):
        self.insert("FIN-1")
        rid = self.c.execute(
            "INSERT INTO domain_review(domain_id,goal,status,requested_at,completed_at,result_json,context_json) "
            "VALUES ('p1','','completed',?,?,?,?)",
            (SNAP, SNAP, json.dumps(result([disposition("FIN-1", "rewrite", title="New title")])),
             json.dumps({"domain": {"id": "p1", "name": "My Domain"}, "goal": "",
                         "tickets": [{"identifier": "FIN-1", "issue_id": "fin-1", "team_key": "TEAM",
                                      "snapshot_updated_at": SNAP, "relationships": {"complete": True,
                                      "fingerprint": "fp"}}],
                         "mirrors": [{"repo": "Finks-ai/finks-ddd", "trunk_sha": "sha1"}]}))).lastrowid
        self.insert("FIN-1", updated_at=SNAP2)  # a new snapshot version, never an in-place edit
        with self.assertRaises(StageError) as cm:
            domain_groom.approve(self.cfg, self.c, rid, ["FIN-1"], "user:dashboard")
        self.assertIn("stale", str(cm.exception))

    def test_merge_approve_writes_state_and_comment_with_target(self):
        self.insert("FIN-1")
        self.insert("FIN-2")
        tickets = [disposition("FIN-1", "merge", target="FIN-2"), disposition("FIN-2", "keep")]
        ctx = {"domain": {"id": "p1", "name": "My Domain"}, "goal": "",
               "tickets": [{"identifier": "FIN-1", "issue_id": "fin-1", "team_key": "TEAM",
                            "snapshot_updated_at": SNAP, "relationships": {"complete": True, "fingerprint": "fp"}},
                           {"identifier": "FIN-2", "issue_id": "fin-2", "team_key": "TEAM",
                            "snapshot_updated_at": SNAP, "relationships": {"complete": True, "fingerprint": "fp"}}],
               "mirrors": [{"repo": "Finks-ai/finks-ddd", "trunk_sha": "sha1"}]}
        rid = self.c.execute(
            "INSERT INTO domain_review(domain_id,goal,status,requested_at,completed_at,result_json,context_json) "
            "VALUES ('p1','','completed',?,?,?,?)",
            (SNAP, SNAP, json.dumps(result(tickets)), json.dumps(ctx))).lastrowid
        detail = domain_groom.approve(self.cfg, self.c, rid, ["FIN-1"], "user:dashboard")
        ops = {w["op"] for w in detail["writebacks"]}
        self.assertEqual(ops, {"state", "comment"})
        state = next(w for w in detail["writebacks"] if w["op"] == "state")
        self.assertEqual(state["payload"]["state"], "Canceled")
        self.assertEqual(state["payload"]["target"], "FIN-2")
        self.assertEqual(state["payload"]["target_issue_id"], "fin-2")
        self.assertEqual(state["payload"]["target_expect_updated_at"], SNAP)

    def review(self, tickets, simplification=None):
        ctx = {"domain": {"id": "p1", "name": "My Domain"}, "goal": "",
               "tickets": [{"identifier": t["identifier"], "issue_id": t["identifier"].lower(),
                            "team_key": "TEAM", "snapshot_updated_at": SNAP,
                            "relationships": {"complete": True, "fingerprint": "fp"}} for t in tickets],
               "mirrors": [{"repo": "Finks-ai/finks-ddd", "trunk_sha": "sha1"}]}
        return self.c.execute("INSERT INTO domain_review(domain_id,goal,status,requested_at,completed_at,result_json,"
                              "context_json) VALUES ('p1','','completed',?,?,?,?)",
                              (SNAP, SNAP, json.dumps(result(tickets, simplification=simplification)),
                               json.dumps(ctx))).lastrowid

    def test_approve_refuses_merge_without_target_rewrite_selected(self):
        self.insert("FIN-1")
        self.insert("FIN-2")
        tickets = [disposition("FIN-1", "merge", target="FIN-2"),
                   disposition("FIN-2", "rewrite", title="T2", description="D\nDomain: My Domain")]
        rid = self.review(tickets)
        with self.assertRaises(StageError) as cm:
            domain_groom.approve(self.cfg, self.c, rid, ["FIN-1"], "user:dashboard")
        self.assertIn("target rewrite", str(cm.exception))
        domain_groom.approve(self.cfg, self.c, rid, ["FIN-1", "FIN-2"], "user:dashboard")  # both together works

    def test_approve_refuses_rewrite_that_drops_domain(self):
        self.insert("FIN-1")
        rid = self.review([disposition("FIN-1", "rewrite", title="T", description="no domain line")])
        with self.assertRaises(StageError) as cm:
            domain_groom.approve(self.cfg, self.c, rid, ["FIN-1"], "user:dashboard")
        self.assertIn("Domain line", str(cm.exception))

    def test_completed_review_evidence_and_result_are_immutable(self):
        self.insert("FIN-1")
        rid = self.complete(tickets=["FIN-1"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("UPDATE domain_review SET result_json='{}' WHERE id=?", (rid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("UPDATE domain_review SET context_json='{}' WHERE id=?", (rid,))

    def test_groom_writeback_content_is_immutable(self):
        self.insert("FIN-1")
        self.c.execute("INSERT INTO writeback(run_id,issue_id,op,payload_json,decision,rule,reason,status) "
                       "VALUES ('domain-1','fin-1','description','{}','apply','domain-groom-rewrite',NULL,'planned')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("UPDATE writeback SET payload_json='{\"title\":\"x\"}' WHERE run_id='domain-1'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("UPDATE writeback SET rule='domain-groom-close' WHERE run_id='domain-1'")
        # a non-groom writeback payload stays editable (legacy behavior unchanged)
        self.c.execute("INSERT INTO writeback(run_id,issue_id,op,payload_json,decision,rule,reason,status) "
                       "VALUES ('sweep-x','fin-1','comment','{}','apply','rule',NULL,'planned')")
        self.c.execute("UPDATE writeback SET payload_json='{\"body\":\"e\"}' WHERE run_id='sweep-x'")

    def test_brief_refuses_held_write(self):
        self.insert("FIN-1")
        self.insert("FIN-2")
        tickets = [disposition("FIN-1", "rewrite", title="A", description="B\nDomain: My Domain"),
                   disposition("FIN-2", "keep")]
        rid = self.review(tickets, simplification={"identifiers": ["FIN-2"], "body": body()})
        domain_groom.approve(self.cfg, self.c, rid, ["FIN-1"], "user:dashboard")
        # a held rewrite is terminal but not actually applied
        self.c.execute("UPDATE writeback SET decision='flag', status='confirmed' WHERE run_id=?", (f"domain-{rid}",))
        with self.assertRaises(StageError) as cm:
            domain_groom.brief(self.cfg, self.c, rid, "user:dashboard")
        self.assertIn("not actually applied", str(cm.exception))

    def test_brief_refuses_mirror_drift(self):
        self.insert("FIN-1")
        self.insert("FIN-2")
        tickets = [disposition("FIN-1", "rewrite", title="A", description="B\nDomain: My Domain"),
                   disposition("FIN-2", "keep")]
        rid = self.review(tickets, simplification={"identifiers": ["FIN-2"], "body": body()})
        domain_groom.approve(self.cfg, self.c, rid, ["FIN-1"], "user:dashboard")
        self.c.execute("UPDATE writeback SET status='confirmed' WHERE run_id=?", (f"domain-{rid}",))
        self.c.execute("UPDATE repo_trunk SET sha='sha2' WHERE repo='Finks-ai/finks-ddd'")
        with self.assertRaises(StageError) as cm:
            domain_groom.brief(self.cfg, self.c, rid, "user:dashboard")
        self.assertIn("moved since the review", str(cm.exception))

    def test_brief_creates_unapproved_idempotent_and_refuses_pending(self):
        self.insert("FIN-1")
        self.insert("FIN-2")
        tickets = [disposition("FIN-1", "rewrite", title="A", description="B\nDomain: My Domain"),
                   disposition("FIN-2", "keep")]
        rid = self.c.execute(
            "INSERT INTO domain_review(domain_id,goal,status,requested_at,completed_at,result_json,context_json) "
            "VALUES ('p1','','completed',?,?,?,?)",
            (SNAP, SNAP, json.dumps(result(tickets, simplification={"identifiers": ["FIN-2"], "body": body()})),
             json.dumps({"domain": {"id": "p1", "name": "My Domain"}, "goal": "",
                         "tickets": [{"identifier": "FIN-1", "issue_id": "fin-1", "team_key": "TEAM",
                                      "snapshot_updated_at": SNAP, "relationships": {"complete": True,
                                      "fingerprint": "fp"}},
                                     {"identifier": "FIN-2", "issue_id": "fin-2", "team_key": "TEAM",
                                      "snapshot_updated_at": SNAP, "relationships": {"complete": True,
                                      "fingerprint": "fp"}}],
                         "mirrors": [{"repo": "Finks-ai/finks-ddd", "trunk_sha": "sha1"}]}))).lastrowid
        # pending (unapplied) writes refuse the brief
        domain_groom.approve(self.cfg, self.c, rid, ["FIN-1"], "user:dashboard")
        with self.assertRaises(StageError) as cm:
            domain_groom.brief(self.cfg, self.c, rid, "user:dashboard")
        self.assertIn("not actually applied", str(cm.exception))
        # reconcile the writes, then the brief is an unapproved draft and idempotent
        self.c.execute("UPDATE writeback SET status='confirmed' WHERE run_id=?", (f"domain-{rid}",))
        b = domain_groom.brief(self.cfg, self.c, rid, "user:dashboard")["brief"]
        self.assertEqual(b["state"], "draft")
        self.assertEqual([s["identifier"] for s in b["sources"]], ["FIN-2"])
        b2 = domain_groom.brief(self.cfg, self.c, rid, "user:dashboard")["brief"]
        self.assertEqual(b2["id"], b["id"])

    def test_brief_requires_human_and_nonnull_simplification(self):
        rid = self.complete(tickets=["FIN-1"])
        with self.assertRaises(StageError):
            domain_groom.brief(self.cfg, self.c, rid, "agent:review")
        with self.assertRaises(StageError):
            domain_groom.brief(self.cfg, self.c, rid, "user:dashboard")  # no simplification proposed


class RecursiveGrooming(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = db.connect(Path(self.tmp.name) / "f.db")
        self.addCleanup(self.c.close)
        self.cfg = Config(raw={"linear": {"lead": LEAD, "team": TEAM}},
                          db=Path(self.tmp.name) / "f.db", mirrors=Path(self.tmp.name) / "m",
                          dispatches=Path(self.tmp.name) / "d",
                          contexts=[Context(name="ctx", repo="Finks-ai/finks-ddd", domains=["My Domain"],
                                            route="fx-news", witnesses=["ch"])],
                          repos={}, witnesses={"ch": {"kind": "clickhouse", "url": "http://x",
                                                      "user_env": "U", "password_env": "P"}})
        self.c.execute("INSERT INTO linear_project(id,slug_id,name,lead_email,fetched_at) VALUES "
                       "('p1','my-domain','My Domain',?,?)", (LEAD, SNAP))
        self.c.execute("INSERT INTO repo_trunk VALUES ('Finks-ai/finks-ddd','main','sha1',?)", (SNAP,))

    def insert(self, ident, updated_at=SNAP):
        stype, r = raw(ident)
        self.c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,?,1,?)",
                       (ident.lower(), ident, updated_at, SNAP, stype, r))
        self.c.execute("INSERT OR REPLACE INTO linear_relationship(identifier, observed_at, edges_json, nodes_json, "
                       "fingerprint) VALUES (?,?,?,?,?)", (ident, SNAP, "[]", "[]", "fp"))

    def envelope(self, outcome, assessment="an assessment", queries=None, review=None):
        return json.dumps({"outcome": outcome, "assessment": assessment,
                           "witness_queries": queries or [], "review": review})

    def ready(self, *idents):
        return self.envelope("ready", review=result([disposition(i, "keep") for i in idents]))

    def _witness(self):
        def side(cfg, conn, name, query):
            cur = conn.execute("INSERT INTO witness_log(witness,kind,query,ok,rows,result_sha256,result_excerpt,at) "
                               "VALUES (?,?,?,1,1,'sha','[[1]]',?)", (name, "clickhouse", query, SNAP))
            return {"witness_log_id": cur.lastrowid, "ok": True, "rows": 1, "result": [[1]]}
        return side

    def _run(self, rid, outputs, witness=False):
        it = iter(outputs)
        runner = lambda argv, **kw: SimpleProc(next(it))
        if witness:
            with mock.patch.object(domain_groom.witness, "run", side_effect=self._witness()):
                return domain_groom.run(self.cfg, self.c, rid, runner=runner)
        return domain_groom.run(self.cfg, self.c, rid, runner=runner)

    def test_manual_ready_records_one_round_and_outcome(self):
        self.insert("FIN-1")
        rid = domain_groom.request(self.cfg, self.c, "p1", spawn=lambda argv, **kw: None)["id"]
        done = self._run(rid, [self.ready("FIN-1")])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["outcome"], "ready")
        self.assertEqual(done["round_count"], 1)
        detail = domain_groom.detail(self.cfg, self.c, rid)
        self.assertEqual([r["outcome"] for r in detail["rounds"]], ["ready"])
        self.assertEqual(len(detail["history"]), 1)
        self.assertEqual(detail["history"][0]["id"], rid)
        self.assertEqual(detail["history"][0]["mode"], "manual")
        # the review summary carries the recursive fields
        self.assertEqual(done["mode"], "manual")
        self.assertIsNone(done["parent_review_id"])
        self.assertIsNone(done["feedback"])

    def test_evidence_then_ready_records_receipts_and_rounds(self):
        self.insert("FIN-1")
        rid = domain_groom.request(self.cfg, self.c, "p1", spawn=lambda argv, **kw: None)["id"]
        evidence = self.envelope("evidence", assessment="need a schema check",
                                 queries=[{"witness": "ch", "query": "SELECT 1"}])
        done = self._run(rid, [evidence, self.ready("FIN-1")], witness=True)
        self.assertEqual(done["outcome"], "ready")
        self.assertEqual(done["round_count"], 2)
        detail = domain_groom.detail(self.cfg, self.c, rid)
        self.assertEqual([(r["number"], r["outcome"]) for r in detail["rounds"]], [(1, "evidence"), (2, "ready")])
        receipt = detail["rounds"][0]["receipts"][0]
        self.assertEqual(receipt["name"], "ch")
        self.assertEqual(receipt["query"], "SELECT 1")
        self.assertTrue(receipt["ok"])
        self.assertIsNotNone(receipt["id"])  # a real witness_log id
        # the witness_log row is durable and cited
        self.assertEqual(self.c.execute("SELECT count(*) FROM witness_log").fetchone()[0], 1)

    def test_blocked_stops_without_result(self):
        self.insert("FIN-1")
        rid = domain_groom.request(self.cfg, self.c, "p1", spawn=lambda argv, **kw: None)["id"]
        blocked = self.envelope("blocked", assessment="no recorded evidence for the consumer")
        done = self._run(rid, [blocked])
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["outcome"], "blocked")
        self.assertIn("no recorded evidence", done["error"])
        self.assertIsNone(domain_groom.detail(self.cfg, self.c, rid)["result"])

    def test_agentic_evidence_budget_yields_limit_reached(self):
        self.insert("FIN-1")
        rid = domain_groom.request(self.cfg, self.c, "p1", mode="agentic", spawn=lambda argv, **kw: None)["id"]
        evidence = self.envelope("evidence", assessment="still checking",
                                 queries=[{"witness": "ch", "query": "SELECT 1"}])
        done = self._run(rid, [evidence, evidence, evidence], witness=True)
        self.assertEqual(done["outcome"], "limit_reached")
        self.assertEqual(done["round_count"], 3)
        self.assertEqual(self.c.execute("SELECT count(*) FROM domain_review_round WHERE review_id=?",
                                        (rid,)).fetchone()[0], 3)

    def test_agentic_critique_finalizes_after_candidate(self):
        self.insert("FIN-1")
        rid = domain_groom.request(self.cfg, self.c, "p1", mode="agentic", spawn=lambda argv, **kw: None)["id"]
        done = self._run(rid, [self.ready("FIN-1"), self.ready("FIN-1")])
        self.assertEqual(done["outcome"], "ready")
        self.assertEqual(done["round_count"], 2)
        # the first ready is a retained DRAFT candidate, the second (critique) finalizes
        rounds = domain_groom.detail(self.cfg, self.c, rid)["rounds"]
        self.assertEqual([(r["number"], r["outcome"]) for r in rounds], [(1, "ready"), (2, "ready")])
        self.assertIsNotNone(rounds[0]["review"])  # candidate retained for audit

    def test_agentic_critique_prompt_carries_prior_ready_assessment(self):
        self.insert("FIN-1")
        rid = domain_groom.request(self.cfg, self.c, "p1", mode="agentic", spawn=lambda argv, **kw: None)["id"]
        first = self.envelope("ready", assessment="FIRST_PASS_ASSESSMENT_MARKER",
                              review=result([disposition("FIN-1", "keep")]))
        critique = self.envelope("ready", assessment="CRITIQUE_PASS_ASSESSMENT_MARKER",
                                 review=result([disposition("FIN-1", "keep")]))
        prompts = []
        it = iter([first, critique])

        def runner(argv, **kw):
            with open(argv[-1][1:]) as handle:  # argv[-1] is @<prompt file>; read the emitted prompt
                prompts.append(handle.read())
            return SimpleProc(next(it))

        done = domain_groom.run(self.cfg, self.c, rid, runner=runner)
        self.assertEqual(done["outcome"], "ready")
        self.assertEqual(done["round_count"], 2)
        self.assertEqual(len(prompts), 2)
        # scan the final (critique) prompt's embedded JSON arrays for the recorded prior-pass summary
        # as a structured object with the correct assessment and pass lineage
        decoded = []
        text = prompts[1]
        for i, ch in enumerate(text):
            if ch != "[":
                continue
            try:
                value, _ = json.JSONDecoder().raw_decode(text[i:])
            except json.JSONDecodeError:
                continue
            decoded.append(value)
        expected = {"number": 1, "assessment": "FIRST_PASS_ASSESSMENT_MARKER", "outcome": "ready"}
        self.assertTrue(any(isinstance(v, list) and expected in v for v in decoded))

    def test_agentic_confirmed_finalizes_with_exact_retained_candidate(self):
        self.insert("FIN-1")
        rid = domain_groom.request(self.cfg, self.c, "p1", mode="agentic", spawn=lambda argv, **kw: None)["id"]
        candidate = result([disposition("FIN-1", "keep")], minimum_system="EXACT_CANDIDATE_MARKER")
        first = self.envelope("ready", assessment="candidate pass", review=candidate)
        confirm = self.envelope("confirmed", assessment="verified unchanged")
        done = self._run(rid, [first, confirm])
        self.assertEqual(done["outcome"], "ready")
        self.assertEqual(done["round_count"], 2)
        detail = domain_groom.detail(self.cfg, self.c, rid)
        rounds = detail["rounds"]
        self.assertEqual([(r["number"], r["outcome"]) for r in rounds], [(1, "ready"), (2, "ready")])
        # the critique round records the EXACT retained candidate, never a regenerated/transcribed copy
        self.assertEqual(rounds[1]["review"], rounds[0]["review"])
        self.assertEqual(detail["result"], rounds[0]["review"])
        self.assertEqual(detail["result"]["minimum_system"], "EXACT_CANDIDATE_MARKER")

    def test_confirmed_rejected_without_retained_candidate(self):
        self.insert("FIN-1")
        # manual mode never retains a candidate, so confirmed cannot finalize
        manual = domain_groom.request(self.cfg, self.c, "p1", spawn=lambda argv, **kw: None)["id"]
        done = self._run(manual, [self.envelope("confirmed", assessment="unchanged")])
        self.assertEqual(done["status"], "failed")
        self.assertIsNone(domain_groom.detail(self.cfg, self.c, manual)["result"])
        # agentic first pass has no candidate yet either
        agentic = domain_groom.request(self.cfg, self.c, "p1", mode="agentic", spawn=lambda argv, **kw: None)["id"]
        done = self._run(agentic, [self.envelope("confirmed", assessment="unchanged")])
        self.assertEqual(done["status"], "failed")

    def test_agentic_child_confirms_inherited_draft_in_one_pass(self):
        self.insert("FIN-1")
        parent = domain_groom.request(self.cfg, self.c, "p1", spawn=lambda argv, **kw: None)["id"]
        self._run(parent, [self.envelope("ready", review=result([disposition("FIN-1", "keep")],
                                                                minimum_system="INHERITED_RESULT_MARKER"))])
        child = domain_groom.request(self.cfg, self.c, "", mode="agentic", parent_review_id=parent,
                                     feedback="please substantiate", spawn=lambda argv, **kw: None)["id"]
        done = self._run(child, [self.envelope("confirmed", assessment="inherited draft verified")])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["outcome"], "ready")
        self.assertEqual(done["round_count"], 1)  # ONE child pass: the inherited draft was critiqued directly
        self.assertIsNone(done["approved_at"])  # never auto-approved
        self.assertEqual(self.c.execute("SELECT count(*) FROM writeback WHERE run_id=?",
                                        (f"domain-{child}",)).fetchone()[0], 0)
        detail = domain_groom.detail(self.cfg, self.c, child)
        parent_detail = domain_groom.detail(self.cfg, self.c, parent)
        self.assertEqual(detail["result"], parent_detail["result"])  # EXACT inherited result, not regenerated
        self.assertEqual(detail["result"]["minimum_system"], "INHERITED_RESULT_MARKER")
        rounds = detail["rounds"]
        self.assertEqual([(r["number"], r["outcome"]) for r in rounds], [(1, "ready")])
        self.assertEqual(rounds[0]["review"], parent_detail["result"])

    def test_zero_pass_failed_parent_carries_nearest_ancestor_draft_and_comment(self):
        self.insert("FIN-1")
        candidate = result([disposition("FIN-1", "keep")], minimum_system="ANCESTOR_DRAFT_MARKER")
        candidate_env = self.envelope("ready", assessment="candidate pass", review=candidate)
        evidence = self.envelope("evidence", assessment="need live facts",
                                 queries=[{"witness": "ch", "query": "SELECT 1"}])
        ancestor = domain_groom.request(self.cfg, self.c, "p1", mode="agentic", spawn=lambda argv, **kw: None)["id"]
        done = self._run(ancestor, [candidate_env, evidence, evidence], witness=True)
        self.assertEqual(done["outcome"], "limit_reached")
        # an intervening child that failed with zero passes (never ran a model turn)
        empty = domain_groom.request(self.cfg, self.c, "", mode="agentic", parent_review_id=ancestor,
                                     feedback="intermediate comment", spawn=lambda argv, **kw: None)["id"]
        self.c.execute("UPDATE domain_review SET status='failed', completed_at=?, error='zero-pass' WHERE id=?",
                       (SNAP, empty))
        child = domain_groom.request(self.cfg, self.c, "", mode="agentic", parent_review_id=empty,
                                     feedback="current comment", spawn=lambda argv, **kw: None)["id"]
        feed = domain_groom._parent_feed(self.c, domain_groom._one(self.c, child))
        self.assertEqual(feed["feedback"], "current comment")  # the current child comment, never the ancestor's
        self.assertTrue(feed["draft"])
        self.assertEqual(feed["result"]["minimum_system"], "ANCESTOR_DRAFT_MARKER")
        self.assertEqual(len(feed["passes"]), 3)  # the nearest ancestor's passes, carried past the empty parent
        self.assertTrue(all(p["review_id"] == ancestor for p in feed["passes"]))

    def test_incompatible_inherited_candidate_cannot_be_confirmed(self):
        self.insert("FIN-1")
        parent = domain_groom.request(self.cfg, self.c, "p1", spawn=lambda argv, **kw: None)["id"]
        self._run(parent, [self.envelope("ready", review=result([disposition("FIN-1", "keep")]))])
        self.insert("FIN-2")  # a new open ticket makes the inherited result incompatible
        child = domain_groom.request(self.cfg, self.c, "", mode="agentic", parent_review_id=parent,
                                     feedback="redo with both tickets", spawn=lambda argv, **kw: None)["id"]
        done = self._run(child, [self.envelope("confirmed", assessment="unchanged")])
        self.assertEqual(done["status"], "failed")  # the stale candidate was not seeded, so confirmed is refused
        self.assertIsNone(domain_groom.detail(self.cfg, self.c, child)["result"])

    def test_agentic_candidate_without_critique_budget_is_limit_reached(self):
        self.insert("FIN-1")
        rid = domain_groom.request(self.cfg, self.c, "p1", mode="agentic", spawn=lambda argv, **kw: None)["id"]
        evidence = self.envelope("evidence", assessment="checking", queries=[{"witness": "ch", "query": "SELECT 1"}])
        # the draft candidate lands on the final pass with no budget left for its critique
        done = self._run(rid, [evidence, evidence, self.ready("FIN-1")], witness=True)
        self.assertEqual(done["outcome"], "limit_reached")
        self.assertEqual(done["status"], "failed")  # never falsely ready
        self.assertEqual(done["round_count"], 3)
        self.assertIsNone(domain_groom.detail(self.cfg, self.c, rid)["result"])
        # the draft candidate is still retained for audit
        rounds = domain_groom.detail(self.cfg, self.c, rid)["rounds"]
        self.assertEqual(rounds[2]["outcome"], "ready")
        self.assertIsNotNone(rounds[2]["review"])

    def test_child_derives_domain_and_preserves_goal(self):
        self.insert("FIN-1")
        parent = domain_groom.request(self.cfg, self.c, "p1", goal="focus on the importer",
                                      spawn=lambda argv, **kw: None)["id"]
        self._run(parent, [self.ready("FIN-1")])
        child = domain_groom.request(self.cfg, self.c, "", mode="manual", parent_review_id=parent,
                                     feedback="please reconsider FIN-1", spawn=lambda argv, **kw: None)["id"]
        row = domain_groom._one(self.c, child)
        self.assertEqual(row["domain_id"], "p1")  # derived server-side, not from the empty body domain_id
        self.assertEqual(row["goal"], "focus on the importer")  # preserved unless explicitly changed
        self.assertEqual(row["feedback"], "please reconsider FIN-1")
        self.assertEqual(row["mode"], "manual")
        self.assertEqual(row["parent_review_id"], parent)

    def test_child_history_is_chronological_lineage(self):
        self.insert("FIN-1")
        parent = domain_groom.request(self.cfg, self.c, "p1", spawn=lambda argv, **kw: None)["id"]
        self._run(parent, [self.ready("FIN-1")])
        child = domain_groom.request(self.cfg, self.c, "", mode="agentic", parent_review_id=parent,
                                     spawn=lambda argv, **kw: None)["id"]
        self._run(child, [self.ready("FIN-1"), self.ready("FIN-1")])
        detail = domain_groom.detail(self.cfg, self.c, child)
        self.assertEqual([h["id"] for h in detail["history"]], [parent, child])

    def test_child_comment_feeds_its_own_round_with_correct_prior_result(self):
        self.insert("FIN-1")
        root = domain_groom.request(self.cfg, self.c, "p1", spawn=lambda argv, **kw: None)["id"]
        self._run(root, [self.envelope("ready", review=result([disposition("FIN-1", "keep")],
                                                              minimum_system="ROOT_RESULT_MARKER"))])
        child = domain_groom.request(self.cfg, self.c, "", mode="manual", parent_review_id=root,
                                     feedback="child comment A", spawn=lambda argv, **kw: None)["id"]
        self._run(child, [self.envelope("ready", review=result([disposition("FIN-1", "keep")],
                                                               minimum_system="CHILD_RESULT_MARKER"))])
        grandchild = domain_groom.request(self.cfg, self.c, "", mode="manual", parent_review_id=child,
                                          feedback="grandchild comment B", spawn=lambda argv, **kw: None)["id"]
        row = domain_groom._one(self.c, grandchild)
        feed = domain_groom._parent_feed(self.c, row)
        self.assertEqual(feed["feedback"], "grandchild comment B")  # the CURRENT comment, never the child's
        self.assertEqual(feed["parent_review_id"], child)
        self.assertEqual(feed["result"]["minimum_system"], "CHILD_RESULT_MARKER")  # prior (immediate parent) result
        # the emitted prompt carries the current comment + prior result, not the grandparent's result or comment
        ctx = json.loads(row["context_json"])
        tmp = tempfile.mkdtemp()
        dw = domain_groom._domain_witnesses(self.cfg, ctx["domain"]["name"])
        prompt = domain_groom._round_prompt(ctx, tmp, parent_feed=feed, prior_rounds=[], receipts=[],
                                            witnesses=dw, candidate=None, mode="manual", number=1,
                                            max_rounds=domain_groom.MANUAL_MAX_ROUNDS)
        self.assertIn("grandchild comment B", prompt)
        self.assertIn("CHILD_RESULT_MARKER", prompt)
        self.assertNotIn("ROOT_RESULT_MARKER", prompt)
        self.assertNotIn("child comment A", prompt)

    def test_failed_parent_carries_draft_and_receipts_to_child(self):
        self.insert("FIN-1")
        cut = {"id": "c1", "title": "cut title", "reason": "cut reason", "evidence": ["cut evidence"],
               "risk": "CUT_RISK_MARKER", "migration": "CUT_MIGRATION_MARKER"}
        candidate = result([disposition("FIN-1", "keep", cut_ids=["c1"])], cuts=[cut],
                           simplification={"identifiers": ["FIN-1"], "body": body(title="SIMPLIFICATION_MARKER")},
                           minimum_system="DRAFT_MINIMUM_MARKER", consumers=["CONSUMER_MARKER"],
                           correctness=["CORRECTNESS_MARKER"])
        candidate_env = self.envelope("ready", assessment="candidate pass", review=candidate)
        evidence = self.envelope("evidence", assessment="need live facts",
                                 queries=[{"witness": "ch", "query": "SELECT COUNT(*) FROM filings"}])
        parent = domain_groom.request(self.cfg, self.c, "p1", mode="agentic", spawn=lambda argv, **kw: None)["id"]

        def witness_side(cfg, conn, name, query):
            cur = conn.execute("INSERT INTO witness_log(witness,kind,query,ok,rows,result_sha256,result_excerpt,at) "
                               "VALUES (?,?,?,1,1,'sha','WITNESS_RESULT_MARKER',?)", (name, "clickhouse", query, SNAP))
            return {"witness_log_id": cur.lastrowid, "ok": True, "rows": 1, "result": [[1]]}

        with mock.patch.object(domain_groom.witness, "run", side_effect=witness_side):
            done = self._run(parent, [candidate_env, evidence, evidence])
        self.assertEqual(done["outcome"], "limit_reached")  # never a false ready
        self.assertEqual(done["status"], "failed")
        self.assertIsNone(domain_groom.detail(self.cfg, self.c, parent)["result"])
        with self.assertRaises(StageError):  # no automatic approval of a failed parent
            domain_groom.approve(self.cfg, self.c, parent, ["FIN-1"], "user:dashboard")

        child = domain_groom.request(self.cfg, self.c, "", mode="manual", parent_review_id=parent,
                                     feedback="carry on from the draft", spawn=lambda argv, **kw: None)["id"]
        feed = domain_groom._parent_feed(self.c, domain_groom._one(self.c, child))
        self.assertTrue(feed["draft"])
        self.assertEqual(feed["result"]["minimum_system"], "DRAFT_MINIMUM_MARKER")
        self.assertEqual(feed["result"]["consumers"], ["CONSUMER_MARKER"])
        self.assertEqual(feed["result"]["cuts"][0]["risk"], "CUT_RISK_MARKER")
        self.assertEqual(feed["result"]["cuts"][0]["migration"], "CUT_MIGRATION_MARKER")
        self.assertEqual(feed["result"]["simplification"]["body"]["title"], "SIMPLIFICATION_MARKER")
        self.assertEqual(len(feed["passes"]), 3)
        self.assertEqual(feed["passes"][0]["outcome"], "ready")

        ctx = json.loads(domain_groom._one(self.c, child)["context_json"])
        tmp = tempfile.mkdtemp()
        dw = domain_groom._domain_witnesses(self.cfg, ctx["domain"]["name"])
        prompt = domain_groom._round_prompt(ctx, tmp, parent_feed=feed, prior_rounds=[], receipts=[],
                                            witnesses=dw, candidate=None, mode="manual", number=1,
                                            max_rounds=domain_groom.MANUAL_MAX_ROUNDS)
        for marker in ("CUT_RISK_MARKER", "CUT_MIGRATION_MARKER", "SIMPLIFICATION_MARKER", "DRAFT_MINIMUM_MARKER",
                       "CONSUMER_MARKER", "SELECT COUNT(*) FROM filings", "WITNESS_RESULT_MARKER"):
            self.assertIn(marker, prompt)

    def test_revised_candidate_retains_distinct_prior_result(self):
        # when the current candidate differs from the prior result, the generated prompt keeps BOTH: the candidate
        # (critique section) and the distinct prior result, because the person's feedback may refer to it
        ctx = {"domain": {"id": "p1", "name": "My Domain", "slug_id": "my-domain"}, "goal": "",
               "tickets": [], "completed_sources": [], "briefs": [], "mirrors": []}
        feed = {"parent_review_id": 7, "feedback": "address the original result",
                "result": result([], minimum_system="ORIGINAL_RESULT_MARKER"), "draft": False, "passes": []}
        tmp = tempfile.mkdtemp()
        dw = domain_groom._domain_witnesses(self.cfg, "My Domain")
        prompt = domain_groom._round_prompt(ctx, tmp, parent_feed=feed, prior_rounds=[], receipts=[],
                                            witnesses=dw,
                                            candidate=result([], minimum_system="REVISED_CANDIDATE_MARKER"),
                                            mode="agentic", number=2,
                                            max_rounds=domain_groom.MAX_AGENTIC_ROUNDS)
        self.assertIn("REVISED_CANDIDATE_MARKER", prompt)  # the current (revised) candidate is shown
        self.assertIn("ORIGINAL_RESULT_MARKER", prompt)    # the distinct prior result is retained

    def test_superseded_unapproved_parent_cannot_be_approved(self):
        self.insert("FIN-1")
        rewrite = self.envelope("ready", review=result(
            [disposition("FIN-1", "rewrite", title="New title", description="New\nDomain: My Domain")]))
        parent = domain_groom.request(self.cfg, self.c, "p1", spawn=lambda argv, **kw: None)["id"]
        self._run(parent, [rewrite])
        child = domain_groom.request(self.cfg, self.c, "", mode="manual", parent_review_id=parent,
                                     spawn=lambda argv, **kw: None)["id"]
        self._run(child, [rewrite])
        with self.assertRaises(StageError) as cm:
            domain_groom.approve(self.cfg, self.c, parent, ["FIN-1"], "user:dashboard")
        self.assertIn("superseded", str(cm.exception))
        # the child itself is still approvable (it has no child of its own)
        domain_groom.approve(self.cfg, self.c, child, ["FIN-1"], "user:dashboard")

    def test_request_refuses_bad_mode_and_parentless_feedback(self):
        with self.assertRaises(StageError):
            domain_groom.request(self.cfg, self.c, "p1", mode="bogus", spawn=lambda argv, **kw: None)
        with self.assertRaises(StageError):
            domain_groom.request(self.cfg, self.c, "p1", feedback="a comment", spawn=lambda argv, **kw: None)

    def test_superseded_by_exposed_on_parent_summary(self):
        self.insert("FIN-1")
        parent = domain_groom.request(self.cfg, self.c, "p1", spawn=lambda argv, **kw: None)["id"]
        self._run(parent, [self.ready("FIN-1")])
        child = domain_groom.request(self.cfg, self.c, "", mode="manual", parent_review_id=parent,
                                     spawn=lambda argv, **kw: None)["id"]
        self._run(child, [self.ready("FIN-1")])
        self.assertEqual(domain_groom.detail(self.cfg, self.c, parent)["superseded_by"], child)
        self.assertIsNone(domain_groom.detail(self.cfg, self.c, child)["superseded_by"])

    def test_superseded_review_refuses_new_brief(self):
        self.insert("FIN-1")
        parent = domain_groom.request(self.cfg, self.c, "p1", spawn=lambda argv, **kw: None)["id"]
        self._run(parent, [self.ready("FIN-1")])
        child = domain_groom.request(self.cfg, self.c, "", mode="manual", parent_review_id=parent,
                                     spawn=lambda argv, **kw: None)["id"]
        self._run(child, [self.ready("FIN-1")])
        with self.assertRaises(StageError) as cm:
            domain_groom.brief(self.cfg, self.c, parent, "user:dashboard")
        self.assertIn("superseded", str(cm.exception))

    def test_partial_approval_after_child_replays_but_refuses_new(self):
        self.insert("FIN-1")
        self.insert("FIN-2")
        rewrites = self.envelope("ready", review=result([
            disposition("FIN-1", "rewrite", title="T1", description="D1\nDomain: My Domain"),
            disposition("FIN-2", "rewrite", title="T2", description="D2\nDomain: My Domain"),
        ]))
        parent = domain_groom.request(self.cfg, self.c, "p1", spawn=lambda argv, **kw: None)["id"]
        self._run(parent, [rewrites])
        run_id = f"domain-{parent}"
        domain_groom.approve(self.cfg, self.c, parent, ["FIN-1"], "user:dashboard")  # partial approval
        self.assertEqual(self.c.execute("SELECT count(*) FROM writeback WHERE run_id=?", (run_id,)).fetchone()[0], 1)
        domain_groom.request(self.cfg, self.c, "", mode="manual", parent_review_id=parent,
                             spawn=lambda argv, **kw: None)["id"]  # child now supersedes the parent
        # exact replay of the already-approved identifier stays idempotent (todo empty, no superseded refusal)
        domain_groom.approve(self.cfg, self.c, parent, ["FIN-1"], "user:dashboard")
        self.assertEqual(self.c.execute("SELECT count(*) FROM writeback WHERE run_id=?", (run_id,)).fetchone()[0], 1)
        # a NEW identifier after the child exists is refused even though approved_at is already set
        with self.assertRaises(StageError) as cm:
            domain_groom.approve(self.cfg, self.c, parent, ["FIN-2"], "user:dashboard")
        self.assertIn("superseded", str(cm.exception))
        # the existing queued write is untouched
        write = self.c.execute("SELECT * FROM writeback WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual(write["issue_id"], "fin-1")
        self.assertEqual(write["status"], "planned")

    def test_record_round_advances_round_count_and_refuses_retired_review(self):
        self.insert("FIN-1")
        rid = domain_groom.request(self.cfg, self.c, "p1", spawn=lambda argv, **kw: None)["id"]
        self.c.execute("UPDATE domain_review SET status='running' WHERE id=?", (rid,))
        review = result([disposition("FIN-1", "keep")])
        domain_groom._record_round(self.c, rid, 1, "candidate", "ready", [], review)
        self.assertEqual(self.c.execute("SELECT round_count FROM domain_review WHERE id=?",
                                        (rid,)).fetchone()[0], 1)  # progress is real, not stuck at 0
        # a retired worker cannot append a late round
        self.c.execute("UPDATE domain_review SET status='failed', completed_at=?, error='retired' WHERE id=?",
                       (SNAP, rid))
        with self.assertRaises(StageError):
            domain_groom._record_round(self.c, rid, 2, "late", "ready", [], review)

    def test_parse_round_output_enforces_the_envelope(self):
        with self.assertRaises(StageError):
            domain_groom._parse_round_output('{"outcome": "bogus", "assessment": "x", '
                                             '"witness_queries": [], "review": null}')
        with self.assertRaises(StageError):  # evidence needs queries
            domain_groom._parse_round_output('{"outcome": "evidence", "assessment": "x", '
                                             '"witness_queries": [], "review": null}')
        with self.assertRaises(StageError):  # ready needs a review object
            domain_groom._parse_round_output('{"outcome": "ready", "assessment": "x", '
                                             '"witness_queries": [], "review": null}')
        # a bare review object (the legacy single-pass shape) is refused for NEW replies
        with self.assertRaises(StageError):
            domain_groom._parse_round_output(json.dumps(result([disposition("FIN-1", "keep")])))

    def test_parse_round_output_confirmed_requires_null_review_and_no_queries(self):
        with self.assertRaises(StageError):  # confirmed with a review is refused
            domain_groom._parse_round_output(json.dumps(
                {"outcome": "confirmed", "assessment": "x", "witness_queries": [],
                 "review": result([disposition("FIN-1", "keep")])}))
        with self.assertRaises(StageError):  # confirmed with witness queries is refused
            domain_groom._parse_round_output('{"outcome": "confirmed", "assessment": "x", '
                                             '"witness_queries": [{"witness": "ch", "query": "SELECT 1"}], '
                                             '"review": null}')
        parsed = domain_groom._parse_round_output('{"outcome": "confirmed", "assessment": "x", '
                                                  '"witness_queries": [], "review": null}')
        self.assertEqual(parsed, {"outcome": "confirmed", "assessment": "x", "witness_queries": [], "review": None})

    def test_witness_receipt_maps_truthfully_on_failure(self):
        def fail(cfg, conn, name, query):
            cur = conn.execute("INSERT INTO witness_log(witness,kind,query,ok,rows,result_sha256,result_excerpt,at) "
                               "VALUES (?,?,?,0,NULL,NULL,?,?)", (name, "clickhouse", query, "unknown table", SNAP))
            return {"witness_log_id": cur.lastrowid, "ok": False, "error": "unknown table"}
        allowed = domain_groom._domain_witnesses(self.cfg, "My Domain")
        with mock.patch.object(domain_groom.witness, "run", side_effect=fail):
            receipt = domain_groom._execute_witnesses(self.cfg, self.c, [{"witness": "ch", "query": "SELECT nope"}],
                                                      allowed)[0]
        self.assertFalse(receipt["ok"])
        self.assertIsNone(receipt["result"])
        self.assertEqual(receipt["error"], "unknown table")

    def test_domain_external_witness_refused(self):
        # a witness configured globally but not mapped to this domain's contexts is refused (never executed)
        self.cfg.witnesses["other"] = {"kind": "clickhouse", "url": "http://x", "user_env": "U", "password_env": "P"}
        allowed = domain_groom._domain_witnesses(self.cfg, "My Domain")
        self.assertEqual(set(allowed), {"ch"})  # only the context-mapped witness is exposed
        with self.assertRaises(StageError) as cm:
            domain_groom._execute_witnesses(self.cfg, self.c, [{"witness": "other", "query": "SELECT 1"}], allowed)
        self.assertIn("not mapped", str(cm.exception))


class SimpleProc:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class GroomReconcile(unittest.TestCase):
    """reconcile.apply sends pinned domain-groom writes with live gates and exact own-updatedAt tracking."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = db.connect(Path(self.tmp.name) / "f.db")
        self.addCleanup(self.c.close)
        self.cfg = Config(raw={"linear": {"lead": LEAD, "team": TEAM}},
                          db=Path(self.tmp.name) / "f.db", mirrors=Path(self.tmp.name) / "m",
                          dispatches=Path(self.tmp.name) / "d",
                          contexts=[Context(name="ctx", repo="Finks-ai/finks-ddd", domains=["My Domain"],
                                            route="fx-news")],
                          repos={}, witnesses={})
        self.c.execute("INSERT INTO linear_project(id,slug_id,name,lead_email,fetched_at) VALUES "
                       "('p1','my-domain','My Domain',?,?)", (LEAD, SNAP))
        self.c.execute("INSERT INTO linear_snapshot VALUES ('fin-1','FIN-1',?,?,'unstarted',1,?)",
                       (SNAP, SNAP, raw("FIN-1")[1]))

    def _live_issue(self, updated_at=SNAP, state="Todo", assignee=None):
        stype = "completed" if state == "Done" else "canceled" if state == "Canceled" else "unstarted"
        return {"id": "fin-1", "identifier": "FIN-1", "title": "Work FIN-1", "description": DOMAIN_BODY,
                "updatedAt": updated_at, "assignee": {"email": assignee} if assignee else None,
                "state": {"name": state, "type": stype}, "labels": {"nodes": []},
                "team": {"id": "t1", "key": "TEAM"}, "project": {"id": "p1", "name": "My Domain"}}

    def _live(self, issue_id="fin-1", updated_at=SNAP, state="Todo", assignee=None):
        stype = "completed" if state == "Done" else "canceled" if state == "Canceled" else "unstarted"
        ident = "FIN-1" if issue_id == "fin-1" else "FIN-2"
        return {"id": issue_id, "identifier": ident, "title": f"Work {ident}", "description": DOMAIN_BODY,
                "updatedAt": updated_at, "assignee": {"email": assignee} if assignee else None,
                "state": {"name": state, "type": stype}, "labels": {"nodes": []},
                "team": {"id": "t1", "key": "TEAM"}, "project": {"id": "p1", "name": "My Domain"}}

    def _queue_issue(self, issue_id, identifier, run_id, op, payload, rule, decision="apply", status="planned"):
        base = {"domain_id": "p1", "identifier": identifier, "expect_updated_at": SNAP}
        self.c.execute("INSERT INTO writeback(run_id,issue_id,op,payload_json,decision,rule,reason,status) "
                       "VALUES (?,?,?,?,?,?,NULL,?)",
                       (run_id, issue_id, op, json.dumps({**base, **payload}), decision, rule, status))

    def _queue(self, run_id, op, payload, rule):
        base = {"domain_id": "p1", "identifier": "FIN-1", "expect_updated_at": SNAP}
        self.c.execute("INSERT INTO writeback(run_id,issue_id,op,payload_json,decision,rule,reason,status) "
                       "VALUES (?,?,?,?,'apply',?,NULL,'planned')",
                       (run_id, "fin-1", op, json.dumps({**base, **payload}), rule))

    def test_rewrite_applies_one_combined_mutation_and_records_its_updated_at(self):
        self._queue("domain-1", "description", {"title": "New title", "description": "New body\nDomain: My Domain"},
                    "domain-groom-rewrite")
        sent = {}

        def gql(cfg, query, variables):
            if "issue(id:" in query:
                return {"issue": self._live_issue()}
            if "issueUpdate" in query:
                sent.update(json.loads(json.dumps(variables))["input"])
                return {"issueUpdate": {"success": True,
                                        "issue": {"updatedAt": "T1", "state": {"name": "Todo"}}}}
            raise AssertionError(query)

        with mock.patch.object(reconcile.linear, "gql", side_effect=gql):
            res = reconcile.apply(self.cfg, self.c, "domain-1")
        self.assertEqual(res["unfinished"], 0)
        row = self.c.execute("SELECT * FROM writeback WHERE run_id='domain-1'").fetchone()
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(sent, {"title": "New title", "description": "New body\nDomain: My Domain"})
        own = [r[1] for r in self.c.execute("SELECT * FROM linear_own_write")]
        self.assertEqual(own, ["T1"])  # exact updatedAt our mutation returned, never re-read

    def test_close_state_then_comment_is_not_staled_by_our_own_cancel(self):
        self._queue("domain-1", "state", {"state": "Canceled"}, "domain-groom-close")
        self._queue("domain-1", "comment", {"body": "closed as unnecessary"}, "domain-groom-close")
        live = {"n": 0}

        def gql(cfg, query, variables):
            if "issue(id:" in query:
                n = live["n"]
                live["n"] += 1
                # the state write reads the fresh ticket; the comment then reads our own canceled T1
                return {"issue": self._live_issue() if n == 0
                        else self._live_issue(updated_at="T1", state="Canceled")}
            if "team(id:" in query:
                return {"team": {"states": {"nodes": [{"name": "Canceled", "id": "s2"}]}}}
            if "issueUpdate" in query:
                return {"issueUpdate": {"success": True,
                                        "issue": {"updatedAt": "T1", "state": {"name": "Canceled"}}}}
            if "commentCreate" in query:
                return {"commentCreate": {"success": True, "comment": {"id": "c1"}}}
            raise AssertionError(query)

        with mock.patch.object(reconcile.linear, "gql", side_effect=gql):
            res = reconcile.apply(self.cfg, self.c, "domain-1")
        self.assertEqual(res["unfinished"], 0)
        rows = {r["op"]: r for r in self.c.execute("SELECT * FROM writeback WHERE run_id='domain-1'")}
        self.assertEqual(rows["state"]["status"], "confirmed")
        self.assertEqual(rows["comment"]["status"], "confirmed")
        self.assertEqual(rows["comment"]["decision"], "apply")  # our own cancel did not hold the follow-up comment
        own = [r[1] for r in self.c.execute("SELECT * FROM linear_own_write")]
        self.assertEqual(own, ["T1"])

    def test_groom_mutation_exception_is_held_not_failed(self):
        self._queue("domain-1", "description", {"title": "New title"}, "domain-groom-rewrite")

        def gql(cfg, query, variables):
            if "issue(id:" in query:
                return {"issue": self._live_issue()}
            if "issueUpdate" in query:
                raise RuntimeError("network dropped mid-send")  # uncertain: the mutation may have landed
            raise AssertionError(query)

        with mock.patch.object(reconcile.linear, "gql", side_effect=gql):
            reconcile.apply(self.cfg, self.c, "domain-1")
        row = self.c.execute("SELECT * FROM writeback WHERE run_id='domain-1'").fetchone()
        self.assertEqual(row["status"], "confirmed")  # held, never blind-retried as 'failed'
        self.assertEqual(row["decision"], "flag")
        self.assertIn("groom send uncertain", row["reason"])
        self.assertTrue(self.c.execute("SELECT 1 FROM decision WHERE kind='writeback' AND run_id='domain-1'").fetchone())

    def test_comment_not_posted_when_state_held(self):
        self._queue("domain-1", "state", {"state": "Canceled"}, "domain-groom-close")
        self._queue("domain-1", "comment", {"body": "closed as unnecessary"}, "domain-groom-close")

        def gql(cfg, query, variables):
            if "issue(id:" in query:
                return {"issue": self._live_issue(assignee="other@finks.ai")}  # gate holds the state write
            raise AssertionError(query)

        with mock.patch.object(reconcile.linear, "gql", side_effect=gql):
            reconcile.apply(self.cfg, self.c, "domain-1")
        state = self.c.execute("SELECT * FROM writeback WHERE run_id='domain-1' AND op='state'").fetchone()
        comment = self.c.execute("SELECT * FROM writeback WHERE run_id='domain-1' AND op='comment'").fetchone()
        self.assertEqual(state["decision"], "flag")
        self.assertEqual(comment["status"], "planned")  # never falsely claims closure

    def test_merge_source_waits_for_skipped_target_rewrite(self):
        self.c.execute("INSERT INTO linear_snapshot VALUES ('fin-2','FIN-2',?,?,'unstarted',1,?)",
                       (SNAP, SNAP, raw("FIN-2")[1]))
        self._queue_issue("fin-2", "FIN-2", "domain-1", "description",
                          {"title": "T2", "description": "D\nDomain: My Domain"}, "domain-groom-rewrite",
                          decision="skip", status="confirmed")  # the user skipped the target rewrite
        self._queue("domain-1", "state", {"state": "Canceled", "target": "FIN-2", "target_issue_id": "fin-2",
                                          "target_expect_updated_at": SNAP}, "domain-groom-merge")
        self._queue("domain-1", "comment", {"body": "merged into FIN-2", "target": "FIN-2",
                                            "target_issue_id": "fin-2", "target_expect_updated_at": SNAP},
                    "domain-groom-merge")
        with mock.patch.object(reconcile.linear, "gql", side_effect=AssertionError):
            res = reconcile.apply(self.cfg, self.c, "domain-1")
        for r in self.c.execute("SELECT op, status FROM writeback WHERE run_id='domain-1' AND op IN ('state','comment')"):
            self.assertEqual(r["status"], "planned")  # never claimed, never sent
        self.assertEqual(res["unfinished"], 2)

    def test_merge_source_cancels_after_target_rewrite_in_same_apply(self):
        self.c.execute("INSERT INTO linear_snapshot VALUES ('fin-2','FIN-2',?,?,'unstarted',1,?)",
                       (SNAP, SNAP, raw("FIN-2")[1]))
        self._queue_issue("fin-2", "FIN-2", "domain-1", "description",
                          {"title": "T2", "description": "D\nDomain: My Domain"}, "domain-groom-rewrite")
        self._queue("domain-1", "state", {"state": "Canceled", "target": "FIN-2", "target_issue_id": "fin-2",
                                          "target_expect_updated_at": SNAP}, "domain-groom-merge")
        self._queue("domain-1", "comment", {"body": "merged into FIN-2", "target": "FIN-2",
                                            "target_issue_id": "fin-2", "target_expect_updated_at": SNAP},
                    "domain-groom-merge")
        seq = []

        def gql(cfg, query, variables):
            if "issue(id:" in query:
                iid = variables["id"]
                return {"issue": self._live(iid)}
            if "team(id:" in query:
                return {"team": {"states": {"nodes": [{"name": "Canceled", "id": "s2"}]}}}
            if "issueUpdate" in query:
                iid = variables["id"]
                seq.append(("update", iid))
                return {"issueUpdate": {"success": True,
                                        "issue": {"updatedAt": "T1" if iid == "fin-1" else "T2",
                                                  "state": {"name": "Canceled" if iid == "fin-1" else "Todo"}}}}
            if "commentCreate" in query:
                seq.append(("comment", variables["input"]["issueId"]))
                return {"commentCreate": {"success": True, "comment": {"id": "c1"}}}
            raise AssertionError(query)

        with mock.patch.object(reconcile.linear, "gql", side_effect=gql):
            res = reconcile.apply(self.cfg, self.c, "domain-1")
        self.assertEqual(res["unfinished"], 0)
        # the target rewrite (fin-2) mutated before the source cancel (fin-1), which is before the comment
        self.assertEqual(seq, [("update", "fin-2"), ("update", "fin-1"), ("comment", "fin-1")])

    def test_externally_changed_target_holds_source_cancel(self):
        self.c.execute("INSERT INTO linear_snapshot VALUES ('fin-2','FIN-2',?,?,'unstarted',1,?)",
                       (SNAP, SNAP, raw("FIN-2")[1]))
        # our target rewrite was confirmed at T2, but the target moved to T3 since (external edit)
        self._queue_issue("fin-2", "FIN-2", "domain-1", "description",
                          {"title": "T2", "description": "D\nDomain: My Domain"}, "domain-groom-rewrite",
                          decision="apply", status="confirmed")
        self.c.execute("UPDATE writeback SET linear_ref='T2' WHERE run_id='domain-1' AND issue_id='fin-2'")
        self._queue("domain-1", "state", {"state": "Canceled", "target": "FIN-2", "target_issue_id": "fin-2",
                                          "target_expect_updated_at": SNAP}, "domain-groom-merge")

        def gql(cfg, query, variables):
            if "issue(id:" in query:
                iid = variables["id"]
                return {"issue": self._live(iid, updated_at="T3") if iid == "fin-2" else self._live(iid)}
            raise AssertionError(query)

        with mock.patch.object(reconcile.linear, "gql", side_effect=gql):
            reconcile.apply(self.cfg, self.c, "domain-1")
        row = self.c.execute("SELECT * FROM writeback WHERE run_id='domain-1' AND op='state'").fetchone()
        self.assertEqual(row["decision"], "flag")
        self.assertIn("changed since the review", row["reason"])

    def test_changed_ticket_is_held_not_sent(self):
        self._queue("domain-1", "description", {"title": "New title"}, "domain-groom-rewrite")

        def gql(cfg, query, variables):
            if "issue(id:" in query:
                return {"issue": self._live_issue(updated_at=SNAP2)}  # changed since the review
            raise AssertionError(query)

        with mock.patch.object(reconcile.linear, "gql", side_effect=gql):
            reconcile.apply(self.cfg, self.c, "domain-1")
        row = self.c.execute("SELECT * FROM writeback WHERE run_id='domain-1' AND op='description'").fetchone()
        self.assertEqual(row["status"], "confirmed")  # held -> flagged -> confirmed (nothing sent)
        self.assertEqual(row["decision"], "flag")
        self.assertIn("apply-time gate", row["reason"])

    def test_resolve_refuses_to_edit_or_hold_pinned_groom_write(self):
        self._queue("domain-1", "description", {"title": "New title"}, "domain-groom-rewrite")
        with self.assertRaises(StageError):
            reconcile.resolve(self.c, "domain-1", "FIN-1", "description", "edited", None)
        with self.assertRaises(StageError):
            reconcile.resolve(self.c, "domain-1", "FIN-1", "description", None, "downgrade")


class Migration(unittest.TestCase):
    def test_v25_adds_domain_tables_and_leaves_writeback_enum_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "f.db"
            raw = sqlite3.connect(path)
            raw.executescript(
                "CREATE TABLE linear_project (id TEXT PRIMARY KEY, slug_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, "
                "lead_email TEXT, fetched_at TEXT NOT NULL);"
                "CREATE TABLE work_brief (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, parent_id INTEGER, "
                "state TEXT NOT NULL, body_json TEXT NOT NULL, sources_json TEXT NOT NULL, created_at TEXT NOT NULL, "
                "created_by TEXT NOT NULL, approved_at TEXT, approved_by TEXT, amendment_reason TEXT, hold_reason TEXT);"
                "CREATE TABLE writeback (run_id TEXT NOT NULL, issue_id TEXT NOT NULL, op TEXT NOT NULL CHECK (op IN "
                "('state','comment','description','create')), payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),"
                " decision TEXT NOT NULL CHECK(decision IN ('apply','skip','flag')), rule TEXT NOT NULL, reason TEXT,"
                " status TEXT NOT NULL CHECK(status IN ('planned','sent','confirmed','failed')), linear_ref TEXT,"
                " approved_by TEXT, PRIMARY KEY(run_id, issue_id, op));"
                "INSERT INTO writeback VALUES ('r','i','state','{}','apply','rule',NULL,'planned',NULL,NULL);"
                "PRAGMA user_version=25;")
            raw.commit()
            raw.close()
            c = db.connect(path)
            self.addCleanup(c.close)
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertEqual(c.execute("SELECT op FROM writeback").fetchone()[0], "state")  # preserved
            self.assertTrue(c.execute("SELECT 1 FROM sqlite_master WHERE name='domain_review'").fetchone())
            self.assertTrue(c.execute("SELECT 1 FROM sqlite_master WHERE name='domain_review_approval'").fetchone())
            # the writeback op enum is untouched: an arbitrary op is still refused by the CHECK
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO writeback VALUES ('r','i','title','{}','apply','r',NULL,'planned',NULL,NULL)")

    def test_v27_adds_recursive_columns_and_round_table(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "f.db"
            raw = sqlite3.connect(path)
            raw.executescript(
                "CREATE TABLE linear_project (id TEXT PRIMARY KEY, slug_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, "
                "lead_email TEXT, fetched_at TEXT NOT NULL);"
                "CREATE TABLE work_brief (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, parent_id INTEGER, "
                "state TEXT NOT NULL, body_json TEXT NOT NULL, sources_json TEXT NOT NULL, created_at TEXT NOT NULL, "
                "created_by TEXT NOT NULL, approved_at TEXT, approved_by TEXT, amendment_reason TEXT, hold_reason TEXT);"
                "CREATE TABLE domain_review (id INTEGER PRIMARY KEY, domain_id TEXT NOT NULL REFERENCES "
                "linear_project(id), goal TEXT NOT NULL DEFAULT '', status TEXT NOT NULL CHECK (status IN "
                "('pending','running','completed','failed')), requested_at TEXT NOT NULL, started_at TEXT, "
                "completed_at TEXT, error TEXT, approved_at TEXT, approved_by TEXT, proposal_brief_id INTEGER "
                "REFERENCES work_brief(id), result_json TEXT, context_json TEXT NOT NULL, CHECK (status IN "
                "('pending','running') OR completed_at IS NOT NULL), CHECK (status <> 'completed' OR result_json IS "
                "NOT NULL), CHECK (status <> 'failed' OR error IS NOT NULL));"
                "CREATE TRIGGER domain_review_frozen BEFORE UPDATE OF result_json, context_json, domain_id, goal ON "
                "domain_review WHEN OLD.status NOT IN ('pending','running') BEGIN SELECT RAISE(ABORT, 'frozen'); END;"
                "INSERT INTO linear_project VALUES ('p1','my-domain','My Domain','me@example.com','2026-09-01T00:00:00.000Z');"
                "INSERT INTO domain_review(domain_id,goal,status,requested_at,completed_at,result_json,context_json) "
                "VALUES ('p1','','completed','2026-09-01T00:00:00.000Z','2026-09-01T00:00:00.000Z',"
                "'{\"minimum_system\":\"m\",\"consumers\":[],\"correctness\":[],\"cuts\":[],\"tickets\":[],"
                "\"simplification\":null,\"limitations\":[]}','{\"domain\":{\"id\":\"p1\"}}');"
                "PRAGMA user_version=26;")
            raw.commit()
            raw.close()
            c = db.connect(path)
            self.addCleanup(c.close)
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertTrue(c.execute("SELECT 1 FROM sqlite_master WHERE name='domain_review_round'").fetchone())
            row = c.execute("SELECT mode, parent_review_id, feedback, round_count, outcome, progress_at "
                            "FROM domain_review WHERE id=1").fetchone()
            self.assertEqual(row["mode"], "manual")  # migrated defaults, evidence intact
            self.assertIsNone(row["parent_review_id"])
            self.assertIsNone(row["outcome"])
            self.assertEqual(row["round_count"], 0)
            # a completed review's mode/parent/feedback are now frozen like its result
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE domain_review SET mode='agentic' WHERE id=1")


if __name__ == "__main__":
    unittest.main()
