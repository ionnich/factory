"""factory.db invariants hold against raw SQL, not just through the CLI. Run: .venv/bin/python -m unittest"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from factory import db
from factory.witness import graphql_read_ok, sql_statement_ok

SNAP = "2026-09-01T00:00:00Z"


class Invariants(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
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

    def test_one_executing_and_auto_done(self):
        self.stage("d1"), self.stage("d2")
        self.x("UPDATE dispatch SET state='executing', last_actor='fm-main' WHERE run_id='d1'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.x("UPDATE dispatch SET state='executing', last_actor='fm-main' WHERE run_id='d2'")
        self.x("UPDATE dispatch_ticket SET card_status='blocked' WHERE run_id='d1'")
        self.assertEqual(self.x("SELECT state FROM dispatch WHERE run_id='d1'").fetchone()[0], "done")
        with self.assertRaises(sqlite3.DatabaseError):
            self.x("UPDATE dispatch_ticket SET card_status='running' WHERE run_id='d1'")
        self.x("UPDATE dispatch SET state='executing', last_actor='fm-main' WHERE run_id='d2'")

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
