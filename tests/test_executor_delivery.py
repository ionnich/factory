"""Recorded executor answers survive delivery failure without reopening or changing the choice."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import db, decide, dispatch


class ExecutorDelivery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "factory.db"
        self.c = self.connect()
        self.cfg = SimpleNamespace(raw={})
        self.c.execute("INSERT INTO dispatch(run_id,state,repos_json,last_actor,created_at) "
                       "VALUES ('run','draft','[]','user',?)", (db.now(),))
        self.c.execute("UPDATE dispatch SET state='staged', approved_by='user', body_sha256='frozen'")
        self.c.execute("UPDATE dispatch SET state='executing', executor_pane='pane-1'")
        self.options = [decide.option("wait", "Wait for the owner", "work pauses"),
                        decide.option("go", "Use the real engine", "the executor changes the engine", note="guidance")]

    def connect(self):
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        return conn

    def question(self, kind="ask"):
        return decide.open_(self.c, kind, "Which engine?", self.options, "wait", "safer", "executor", run_id="run")

    def test_failure_survives_reconnect_and_resend_keeps_exact_answer_in_current_pane(self):
        did = self.question()
        with mock.patch.object(dispatch, "_send", side_effect=dispatch.StageError("pane unavailable")) as send:
            result = decide.choose(self.cfg, self.c, did, "go", "user:dashboard", note="keep the audit log")
        message = send.call_args.args[1][0]
        self.assertIn("pane unavailable", result["after_error"])
        conn = self.connect()
        original = decide.one(conn, did)
        self.assertEqual((original["chosen"], original["chosen_note"], original["chosen_by"], original["open"]),
                         ("go", "keep the audit log", "user:dashboard", False))
        [delivery] = decide.executor_deliveries(conn)
        self.assertEqual((delivery["decision_id"], delivery["answer"], delivery["state"]),
                         (did, "Use the real engine", "failed"))
        self.assertIn("pane unavailable", delivery["error"])
        self.assertIsNotNone(delivery["attempted_at"])
        self.assertIsNone(delivery["sent_at"])
        self.assertNotIn("message", delivery)
        conn.execute("UPDATE dispatch SET executor_pane='pane-2'")
        with mock.patch.object(dispatch, "_send") as send:
            self.assertEqual(decide.resend(conn, did), {"decision": did, "sent_to_executor": "pane-2"})
            send.assert_called_once_with("pane-2", [message])
            with self.assertRaises(dispatch.StageError):
                decide.resend(conn, did)
            with self.assertRaises(dispatch.StageError):
                decide.choose(self.cfg, conn, did, "wait", "user:cli")
            send.assert_called_once()
        self.assertEqual(decide.executor_deliveries(conn), [])
        self.assertEqual(decide.one(conn, did), original)
        sent = conn.execute("SELECT state, error, sent_at FROM executor_delivery WHERE decision_id=?", (did,)).fetchone()
        self.assertEqual(tuple(sent)[:2], ("sent", None))
        self.assertIsNotNone(sent["sent_at"])

    def test_answer_and_pending_delivery_commit_together(self):
        did = self.question()
        with mock.patch.object(decide, "resend", side_effect=dispatch.StageError("sender not started")):
            decide.choose(self.cfg, self.c, did, "wait", "user")
        conn = self.connect()
        self.assertEqual(decide.one(conn, did)["chosen"], "wait")
        self.assertEqual(decide.executor_deliveries(conn)[0]["state"], "pending")
        another = self.question()
        self.c.execute("CREATE TRIGGER fail_delivery BEFORE INSERT ON executor_delivery "
                       "BEGIN SELECT RAISE(ABORT, 'cannot record delivery'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            decide.choose(self.cfg, self.c, another, "wait", "user")
        self.assertTrue(decide.one(conn, another)["open"])
        self.assertIsNone(conn.execute("SELECT 1 FROM executor_delivery WHERE decision_id=?", (another,)).fetchone())

    def test_unanswered_nonask_missing_and_terminal_refused(self):
        unanswered, other = self.question(), self.question("blocked")
        decide.choose(self.cfg, self.c, other, "wait", "user")
        with mock.patch.object(dispatch, "_send") as send:
            for did in (unanswered, other, 99999):
                with self.subTest(did=did), self.assertRaises(dispatch.StageError):
                    decide.resend(self.c, did)
            send.assert_not_called()
        with mock.patch.object(dispatch, "_send", side_effect=dispatch.StageError("offline")):
            decide.choose(self.cfg, self.c, unanswered, "wait", "user")
        self.c.execute("UPDATE dispatch SET state='done'")
        with mock.patch.object(dispatch, "_send") as send, self.assertRaises(dispatch.StageError):
            decide.resend(self.c, unanswered)
        send.assert_not_called()
        self.assertEqual(decide.executor_deliveries(self.c), [])

    def test_claim_is_committed_before_send_and_refuses_concurrent_resend(self):
        did = self.question()
        conn = self.connect()

        def during_send(pane, texts):
            self.assertEqual(decide.one(conn, did)["chosen"], "wait")
            self.assertEqual(decide.executor_deliveries(conn)[0]["state"], "sending")
            with self.assertRaisesRegex(dispatch.StageError, "concurrent resend refused"):
                decide.resend(conn, did)

        with mock.patch.object(dispatch, "_send", side_effect=during_send) as send:
            decide.choose(self.cfg, self.c, did, "wait", "user")
        send.assert_called_once()
        self.assertEqual(decide.executor_deliveries(conn), [])

    def test_interrupted_sender_requires_absence_then_another_explicit_resend(self):
        did = self.question()
        with mock.patch.object(dispatch, "_send", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            decide.choose(self.cfg, self.c, did, "wait", "user")
        conn = self.connect()
        with mock.patch.object(dispatch, "_send") as send:
            with self.assertRaisesRegex(dispatch.StageError, "concurrent resend refused"):
                decide.resend(conn, did)
            with mock.patch.object(decide.os, "kill", side_effect=ProcessLookupError):
                result = decide.resend(conn, did)
            self.assertIn("may duplicate", result["after_error"])
            send.assert_not_called()
            [delivery] = decide.executor_deliveries(self.connect())
            self.assertEqual(delivery["state"], "failed")
            self.assertIn("may duplicate", delivery["error"])
            decide.resend(conn, did)
            send.assert_called_once()

    def test_executor_choices_never_earn_automatic_answers(self):
        with mock.patch.object(dispatch, "_send"):
            for _ in range(decide.EARNED_AFTER):
                decide.choose(self.cfg, self.c, self.question(), "wait", "user")
        question = decide.one(self.c, self.question())
        self.assertEqual(question["tier"], "now")
        self.assertIsNone(question["deadline"])
        self.assertTrue(all(option["weighty"] for option in question["options"]))

    def test_v16_answer_is_unknown_not_silently_replayed(self):
        did = self.question()
        self.c.execute("UPDATE decision SET chosen='go', chosen_by='user', chosen_at=?, chosen_note='original note' "
                       "WHERE id=?", (db.now(), did))
        self.c.execute("DROP TABLE executor_delivery")
        self.c.executescript("BEGIN;\n" + db.MIGRATIONS[17] + "\nCOMMIT;")
        with mock.patch.object(dispatch, "_send") as send:
            conn = self.connect()
            [delivery] = decide.executor_deliveries(conn)
            self.assertEqual((delivery["decision_id"], delivery["state"]), (did, "failed"))
            self.assertIn("predates durable tracking", delivery["error"])
            send.assert_not_called()
            decide.resend(conn, did)
            text = send.call_args.args[1][0]
        self.assertIn("Use the real engine. Note: original note (by user;", text)
        self.assertEqual(decide.executor_deliveries(conn), [])


if __name__ == "__main__":
    unittest.main()
