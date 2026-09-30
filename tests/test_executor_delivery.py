"""Recorded executor answers survive delivery failure without reopening or changing the choice. While the work waits
on a person (an open question, an answer not yet delivered) the gone-quiet alert pauses; the executor-gone one never
does."""
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory import cli, db, decide, dispatch


def ago(hours: float) -> str:
    return decide._iso(datetime.now(UTC) - timedelta(hours=hours))


class ExecutorDelivery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "factory.db"
        self.c = self.connect()
        self.cfg = SimpleNamespace(raw={}, dispatches=Path(self.tmp.name))
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
        with mock.patch.object(decide, "_tier", return_value="auto"):  # Stored before executor confirmation.
            legacy = self.question()
        self.assertEqual(decide.one(self.c, legacy)["tier"], "now")
        self.assertEqual(self.c.execute("SELECT tier FROM decision WHERE id=?", (legacy,)).fetchone()[0], "auto")

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

    def watch(self, *live_panes):
        panes = {"result": {"panes": [{"pane_id": p, "agent": "omp"} for p in live_panes]}}
        with mock.patch.object(dispatch, "_herdr", return_value=panes):
            return [r["kind"] for r in dispatch.watch(self.cfg, self.c)]

    def test_open_question_withdraws_the_quiet_alert_never_the_crash_alert(self):
        self.c.execute("UPDATE dispatch SET executing_at=?", (ago(8),))
        quiet = decide.executor(self.c, "run", "dispatch-stuck", "no card activity for 6.1h (limit 6h)", 6)
        asked = self.question()
        self.assertEqual(self.watch(), ["executor-gone"])  # pane-1 is gone; 8h quiet, but a question waits on a person
        withdrawn = decide.one(self.c, quiet)
        self.assertEqual((withdrawn["open"], withdrawn["chosen"]), (False, None))
        self.assertTrue(decide.one(self.c, asked)["open"])

    def test_a_due_quiet_alert_is_withdrawn_not_answered_while_a_question_waits(self):
        with mock.patch.object(decide, "_tier", return_value="auto"):  # its ★ is due, before watch() withdraws it
            quiet = decide.executor(self.c, "run", "dispatch-stuck", "no card activity for 6.1h (limit 6h)", 6)
        asked = self.question()
        [swept] = decide.sweep(self.cfg, self.c)
        withdrawn = decide.one(self.c, quiet)
        self.assertEqual((swept["id"], withdrawn["open"], withdrawn["chosen"]), (quiet, False, None))
        self.assertIn(f"decision #{asked}", withdrawn["void_reason"])
        self.assertTrue(decide.one(self.c, asked)["open"])

    def test_no_answer_acts_on_a_quiet_alert_while_an_answer_is_undelivered_a_crash_alert_still_answers(self):
        quiet = decide.executor(self.c, "run", "dispatch-stuck", "no card activity for 6.1h (limit 6h)", 6)
        gone = decide.executor(self.c, "run", "executor-gone", "pane gone", 6)
        did = self.question()
        with mock.patch.object(dispatch, "_send", side_effect=dispatch.StageError("pane busy")):
            decide.choose(self.cfg, self.c, did, "wait", "user")
        with self.assertRaisesRegex(dispatch.StageError, f"decision #{did}"):
            decide.choose(self.cfg, self.c, quiet, "wait", "user:dashboard")
        self.assertTrue(decide.one(self.c, quiet)["open"])
        self.assertEqual(decide.choose(self.cfg, self.c, gone, "wait", "user:dashboard")["chosen"], "wait")

    def test_quiet_clock_resumes_at_delivery_not_at_the_choice_or_a_failed_send(self):
        self.c.execute("UPDATE dispatch SET executing_at=?", (ago(9),))
        did = self.question()
        with mock.patch.object(db, "now", return_value=ago(8)), \
                mock.patch.object(dispatch, "_send", side_effect=dispatch.StageError("pane busy")):
            decide.choose(self.cfg, self.c, did, "wait", "user")
        self.assertEqual(self.watch("pane-1"), [])  # answered and failed 8h ago: still waits on a person
        with mock.patch.object(dispatch, "_send"):
            decide.resend(self.c, did)
        self.assertEqual(self.watch("pane-1"), [])  # 9h since the start, but the answer just arrived
        self.c.execute("UPDATE executor_delivery SET sent_at=?", (ago(7),))
        self.assertEqual(self.watch("pane-1"), ["dispatch-stuck"])

    def test_run_status_names_the_blocker_that_matters_most(self):
        started = ago(1)
        self.c.execute("UPDATE dispatch SET executing_at=?", (started,))
        run = lambda: cli.dispatch_status(self.cfg, self.c, "run")["runtime"]
        blocked_by = lambda: ((r := run())["decision_id"], r["blocker"] is not None)
        idle = run()
        self.assertEqual((idle["last_activity_at"], idle["blocker"], idle["decision_id"]), (started, None, None))
        quiet = decide.executor(self.c, "run", "dispatch-stuck", "quiet", 6)
        self.assertEqual(blocked_by(), (quiet, True))
        older, newer = self.question(), self.question()
        self.assertEqual(blocked_by(), (older, True))  # an open question outranks gone quiet
        with mock.patch.object(dispatch, "_send", side_effect=dispatch.StageError("pane busy")):
            decide.choose(self.cfg, self.c, newer, "wait", "user")
        self.assertEqual(blocked_by(), (newer, True))  # an answer that never arrived outranks an open question
        gone = decide.executor(self.c, "run", "executor-gone", "pane gone", 6)
        self.assertEqual(blocked_by(), (gone, True))  # a crash outranks everything
        with mock.patch.object(dispatch, "_send"):
            decide.resend(self.c, newer)
        sent = self.c.execute("SELECT sent_at FROM executor_delivery").fetchone()[0]
        self.assertEqual(run()["last_activity_at"], sent)  # the delivered answer is the latest real activity


if __name__ == "__main__":
    unittest.main()
