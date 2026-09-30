"""Ticket timeline: one ticket's history across tables, oldest first. Run: .venv/bin/python -m unittest"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from factory import db
from factory.cli import ticket_timeline


def raw(state, title="Fix it"):
    return json.dumps({"state": {"name": state}, "assignee": None, "priority": 2, "title": title,
                       "description": "", "labels": {"nodes": []}})


class Timeline(unittest.TestCase):
    def test_orders_everything_the_factory_did(self):
        c = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        T0, T1, T2 = "2020-01-01T00:00:00.000Z", "2020-01-02T00:00:00.000Z", "2020-01-03T00:00:00.000Z"
        c.execute("INSERT INTO linear_snapshot VALUES ('i1','FIN-1',?,?,'unstarted',1,?)", (T0, T0, raw("Todo")))
        ev = json.dumps([{"type": "file", "path": "a.py"}])
        for vid, at, kind, sup in ((1, "2020-01-01T01:00:00.000000Z", "needs-clarification", T1),
                                   (2, T1, "valid", None)):
            c.execute("INSERT INTO verdict(id, issue_id, snapshot_updated_at, kind, reason, evidence_json, created_at, "
                      "created_by, superseded_at) VALUES (?, 'i1', ?, ?, 'r', ?, ?, 'agent:factory-prune', ?)",
                      (vid, T0, kind, ev, at, sup))
        c.execute("INSERT INTO dispatch(run_id, state, repos_json, last_actor, created_at) "
                  "VALUES ('d1','draft','[]','user',?)", (T2,))
        c.execute("INSERT INTO dispatch_ticket(run_id, issue_id, identifier, snapshot_updated_at, verdict_id) "
                  "VALUES ('d1','i1','FIN-1',?,2)", (T0,))
        c.execute("UPDATE dispatch SET state='staged', body_sha256='x', approved_by='user' WHERE run_id='d1'")
        c.execute("UPDATE dispatch SET state='executing', last_actor='executor' WHERE run_id='d1'")
        time.sleep(0.002)  # transitions stamp sqlite 'now' (ms); keep the card after them, as in a real run
        c.execute("INSERT INTO card_event(run_id, issue_id, kind, actor, body, metadata_json, at) "
                  "VALUES ('d1','i1','done','executor','shipped',?,?)", (json.dumps({"pr": "https://x/pull/1"}), db.now()))
        c.execute("UPDATE dispatch_ticket SET card_status='done' WHERE run_id='d1'")  # closes the dispatch
        c.execute("INSERT INTO writeback(run_id, issue_id, op, payload_json, decision, rule, status) "
                  "VALUES ('d1','i1','state','{}','flag','card-done','confirmed')")
        c.execute("INSERT INTO writeback(run_id, issue_id, op, payload_json, decision, rule, status) "
                  "VALUES ('sweep-20990101-000000','i1','comment','{}','apply','valid','confirmed')")

        t = ticket_timeline(c, "i1")
        self.assertEqual([(e["kind"], e["summary"].split(":")[0]) for e in t], [
            ("linear", "ingested in Todo"),
            ("verdict", "needs-clarification (superseded)"), ("verdict", "valid"),
            ("dispatch", "d1"), ("dispatch", "d1"), ("dispatch", "d1"),
            ("card", "done"), ("dispatch", "d1"),
            ("writeback", "state held (card-done)"),
            ("writeback", "comment applied (valid)"),
        ])
        self.assertEqual([e["summary"] for e in t if e["kind"] == "dispatch"],
                         ["d1: new → draft", "d1: draft → staged", "d1: staged → executing", "d1: executing → done"])
        self.assertEqual(t[6]["detail"]["pr"], "https://x/pull/1")
        self.assertEqual(t[1]["detail"]["superseded_at"], T1)


if __name__ == "__main__":
    unittest.main()
