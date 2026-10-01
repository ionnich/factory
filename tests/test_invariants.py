"""factory.db invariants hold against raw SQL, not just through the CLI. Run: .venv/bin/python -m unittest"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import _v21  # noqa: E402

from factory import db
from factory.witness import graphql_read_ok, sql_statement_ok

SNAP = "2026-09-01T00:00:00Z"


class Invariants(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        _v21.ensure_schema(self.c)
        for i in (1, 2):
            self.c.execute("INSERT INTO linear_snapshot VALUES (?,?,?,?,?,1,'{}')", (f"i{i}", f"FIN-{i}", SNAP, SNAP, "unstarted"))
            self.c.execute("INSERT INTO verdict(issue_id,snapshot_updated_at,kind,reason,evidence_json,created_at,created_by) "
                           "VALUES (?,?,'valid','r','[1]',?,'t')", (f"i{i}", SNAP, SNAP))
            self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) VALUES (?,'draft','[]','p',?)", (f"d{i}", SNAP))
            self.c.execute("INSERT INTO dispatch_ticket(run_id,issue_id,identifier,snapshot_updated_at,verdict_id) VALUES (?,?,?,?,?)",
                           (f"d{i}", f"i{i}", f"FIN-{i}", SNAP, i))

    def x(self, sql, *a):
        return self.c.execute(sql, a)

    def stage(self, run):
        self.x("UPDATE dispatch SET state='staged', body_sha256='h', approved_by='u', last_actor='p' WHERE run_id=?", run)

    def test_forward_edges_only(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.x("UPDATE dispatch SET state='executing', body_sha256='h' WHERE run_id='d1'")
        self.stage("d1")
        with self.assertRaises(sqlite3.DatabaseError):
            self.x("UPDATE dispatch SET state='done' WHERE run_id='d1'")

    def test_immutable_once_staged(self):
        self.stage("d1")
        for sql in ("UPDATE dispatch SET body_sha256='z' WHERE run_id='d1'",
                    "DELETE FROM dispatch_ticket WHERE run_id='d1'",
                    "DELETE FROM dispatch WHERE run_id='d1'"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                self.x(sql)

    def test_capacity_guard_and_auto_done(self):
        self.stage("d1"), self.stage("d2")
        self.x("UPDATE execution_policy SET max_parallel=1")  # one_executing replaced by the bounded capacity guard
        self.x("UPDATE dispatch SET state='executing', last_actor='fm-main' WHERE run_id='d1'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.x("UPDATE dispatch SET state='executing', last_actor='fm-main' WHERE run_id='d2'")
        self.x("UPDATE dispatch_ticket SET card_status='blocked' WHERE run_id='d1'")
        self.assertEqual(self.x("SELECT state FROM dispatch WHERE run_id='d1'").fetchone()[0], "done")
        with self.assertRaises(sqlite3.DatabaseError):
            self.x("UPDATE dispatch_ticket SET card_status='running' WHERE run_id='d1'")
        self.x("UPDATE dispatch SET state='executing', last_actor='fm-main' WHERE run_id='d2'")  # d1 done: slot freed

    def test_launch_reserve_state_and_edges(self):
        with self.assertRaises(sqlite3.DatabaseError):  # a launch is reserved only for a staged dispatch
            self.x("INSERT INTO dispatch_launch(run_id,pane_id,state,claimed_at) VALUES ('d1','p1','reserved',?)", SNAP)
        self.stage("d1")
        self.x("INSERT INTO dispatch_launch(run_id,pane_id,state,claimed_at) VALUES ('d1','p1','reserved',?)", SNAP)
        self.x("UPDATE dispatch_launch SET state='sent' WHERE run_id='d1'")  # reserved -> sent
        with self.assertRaises(sqlite3.IntegrityError):  # sent has no outgoing edge
            self.x("UPDATE dispatch_launch SET state='uncertain' WHERE run_id='d1'")

    def test_launch_release_guard(self):
        self.stage("d1")
        self.x("INSERT INTO dispatch_launch(run_id,pane_id,state,claimed_at) VALUES ('d1','p1','reserved',?)", SNAP)
        self.x("UPDATE dispatch_launch SET state='uncertain' WHERE run_id='d1'")  # reserved -> uncertain
        with self.assertRaises(sqlite3.DatabaseError):  # uncertain delete only once terminal
            self.x("DELETE FROM dispatch_launch WHERE run_id='d1'")
        self.x("UPDATE dispatch_launch SET state='reserved' WHERE run_id='d1'")  # uncertain -> reserved (recovery)
        self.x("DELETE FROM dispatch_launch WHERE run_id='d1'")  # reserved deletes anytime

    def test_resource_held_to_archive(self):
        self.x("INSERT INTO dispatch_resource(run_id, resource) VALUES ('d1','repo:a')")  # draft: pinned
        self.stage("d1")
        with self.assertRaises(sqlite3.DatabaseError):  # claims held until archive
            self.x("DELETE FROM dispatch_resource WHERE run_id='d1'")
        with self.assertRaises(sqlite3.DatabaseError):  # immutable after draft
            self.x("INSERT INTO dispatch_resource(run_id, resource) VALUES ('d1','repo:b')")

    def test_no_double_booking(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.x("INSERT INTO dispatch_ticket(run_id,issue_id,identifier,snapshot_updated_at,verdict_id) "
                   "VALUES ('d2','i1','FIN-1',?,1)", SNAP)

    def test_audit_log_append_only(self):
        self.stage("d1")
        self.assertEqual([tuple(r) for r in self.x("SELECT from_state,to_state FROM transition_log WHERE run_id='d1'")],
                         [(None, "draft"), ("draft", "staged")])
        with self.assertRaises(sqlite3.DatabaseError):
            self.x("DELETE FROM transition_log")

    def test_history_never_deleted(self):
        for sql in ("DELETE FROM verdict WHERE id=2", "DELETE FROM dispatch WHERE run_id='d2'"):  # d2 is a draft
            with self.subTest(sql=sql), self.assertRaisesRegex(sqlite3.DatabaseError, "never deleted"):  # not the FK
                self.x(sql)

    def test_confirmed_writeback_final(self):
        self.x("INSERT INTO writeback(run_id,issue_id,op,payload_json,decision,rule,status) "
               "VALUES ('d1','i1','state','{}','flag','r','confirmed')")
        with self.assertRaises(sqlite3.DatabaseError):
            self.x("UPDATE writeback SET status='planned' WHERE run_id='d1'")
        self.x("UPDATE writeback SET decision='apply', status='planned', approved_by='u' WHERE run_id='d1'")  # apply anyway

    def test_witness_statement_gates(self):
        drop = "DR" + "OP TABLE t"
        for q, ok in [("SELECT 1", True), ("show tables", True), ("DESCRIBE t", True), (drop, False),
                      (f"SELECT 1; {drop}", False), ("INSERT INTO t SELECT 1", False),
                      ("SELECT 1 INTO OUTFILE 'x'", False)]:
            self.assertEqual(sql_statement_ok(q), ok, q)
        self.assertFalse(graphql_read_ok("mutation { launchRun { ok } }"))
        self.assertTrue(graphql_read_ok("{ version }"))

class Ownership(unittest.TestCase):
    """Only tickets whose Domain: project is led by linear.lead are in scope; other leads' tickets are never touched."""

    def test_domain_link_resolution(self):
        import json
        from factory import prune
        from factory.config import Config
        c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        uuid = "5e876ee6-5818-4a40-9af1-e45e15a49231"
        c.executemany("INSERT INTO linear_project VALUES (?,?,?,?,?)", [
            ("p1", "44941f276414", "Entity Graph", "me@x", SNAP),
            (uuid, "aaaaaaaaaaaa", "Discover", "other@x", SNAP),
            ("p3", "bbbbbbbbbbbb", "Insights", "me@x", SNAP)])
        cfg = Config(raw={"linear": {"lead": "me@x"}}, db=Path("x"), mirrors=Path("x"), dispatches=Path("x"),
                     contexts=[], repos={}, witnesses={})

        def snap(body):
            return {"identifier": "FIN-1", "raw_json": json.dumps({"description": body, "labels": {"nodes": []}})}
        cases = [
            ("Domain: [Entity Graph](https://linear.app/j/project/entity-graph-44941f276414)", True),  # slug id
            (f"Domain: [Discover](<https://linear.app/j/project/discover-{uuid}>)", False),           # uuid, other lead
            ("**Domain:** Insights", True),                                                           # name fallback
            ("Domain: [Renamed](https://linear.app/j/project/x-44941f276414)", True),                 # link beats name
            ("no domain line", False),
        ]
        for body, want in cases:
            self.assertEqual(prune.owned(cfg, c, snap(body)), want, body)



if __name__ == "__main__":
    unittest.main()
