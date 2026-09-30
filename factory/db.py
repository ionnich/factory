"""SQLite connection and schema bootstrap."""
import sqlite3
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

SCHEMA_VERSION = 19

# Upgrades for existing DBs; schema.sql always holds the full current schema for fresh ones.
MIGRATIONS = {
    2: """CREATE TABLE linear_project (
  id TEXT PRIMARY KEY, slug_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  lead_email TEXT, fetched_at TEXT NOT NULL);""",
    3: """DROP TABLE writeback;
CREATE TABLE writeback (
  run_id TEXT NOT NULL, issue_id TEXT NOT NULL,
  op TEXT NOT NULL CHECK (op IN ('state', 'comment', 'description')),
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  decision TEXT NOT NULL CHECK (decision IN ('apply', 'skip', 'flag')),
  rule TEXT NOT NULL, reason TEXT,
  status TEXT NOT NULL CHECK (status IN ('planned', 'sent', 'confirmed', 'failed')),
  linear_ref TEXT, PRIMARY KEY (run_id, issue_id, op));
CREATE TRIGGER writeback_no_upgrade BEFORE UPDATE OF decision ON writeback
WHEN NEW.decision IS NOT OLD.decision AND NOT (OLD.decision = 'apply' AND NEW.decision = 'flag')
BEGIN SELECT RAISE(ABORT, 'writeback decision may only be downgraded apply -> flag'); END;""",
    4: """CREATE TABLE linear_own_write (issue_id TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (issue_id, updated_at));""",
    5: """ALTER TABLE writeback RENAME TO writeback_v4;
CREATE TABLE writeback (
  run_id TEXT NOT NULL, issue_id TEXT NOT NULL,
  op TEXT NOT NULL CHECK (op IN ('state', 'comment', 'description', 'create')),
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  decision TEXT NOT NULL CHECK (decision IN ('apply', 'skip', 'flag')),
  rule TEXT NOT NULL, reason TEXT,
  status TEXT NOT NULL CHECK (status IN ('planned', 'sent', 'confirmed', 'failed')),
  linear_ref TEXT, PRIMARY KEY (run_id, issue_id, op));
INSERT INTO writeback SELECT * FROM writeback_v4;
DROP TABLE writeback_v4;
CREATE TRIGGER writeback_no_upgrade BEFORE UPDATE OF decision ON writeback
WHEN NEW.decision IS NOT OLD.decision AND NOT (OLD.decision = 'apply' AND NEW.decision = 'flag')
BEGIN SELECT RAISE(ABORT, 'writeback decision may only be downgraded apply -> flag'); END;""",
    6: """ALTER TABLE dispatch ADD COLUMN drafted_by TEXT;
ALTER TABLE dispatch ADD COLUMN planned_at TEXT;
ALTER TABLE dispatch ADD COLUMN notified_at TEXT;
ALTER TABLE dispatch ADD COLUMN review_until TEXT;
ALTER TABLE dispatch ADD COLUMN held_reason TEXT;
ALTER TABLE dispatch ADD COLUMN approved_by TEXT;
ALTER TABLE dispatch ADD COLUMN rejected_reason TEXT;
ALTER TABLE dispatch ADD COLUMN emergency INTEGER NOT NULL DEFAULT 0;
UPDATE dispatch SET drafted_by = 'user', approved_by = last_actor WHERE state <> 'draft';
DROP TRIGGER dispatch_edges;
CREATE TRIGGER dispatch_edges BEFORE UPDATE OF state ON dispatch
WHEN NEW.state IS NOT OLD.state AND (OLD.state, NEW.state) NOT IN (VALUES
  ('draft', 'staged'), ('staged', 'executing'), ('executing', 'done'),
  ('done', 'reconciled'), ('reconciled', 'archived'), ('draft', 'archived'))
BEGIN SELECT RAISE(ABORT, 'illegal dispatch transition'); END;
CREATE TRIGGER dispatch_review_gate BEFORE UPDATE OF state ON dispatch
WHEN OLD.state = 'draft' AND ((NEW.state = 'staged' AND NEW.approved_by IS NULL)
  OR (NEW.state = 'archived' AND NEW.rejected_reason IS NULL))
BEGIN SELECT RAISE(ABORT, 'a draft leaves review only approved (staged) or rejected with a reason'); END;
CREATE TABLE dispatch_step (
  run_id          TEXT NOT NULL REFERENCES dispatch(run_id),
  step_id         TEXT NOT NULL,
  title           TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  detail          TEXT NOT NULL DEFAULT '',
  depends_on_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(depends_on_json)),
  PRIMARY KEY (run_id, step_id)
);
CREATE TRIGGER dispatch_step_draft_only BEFORE INSERT ON dispatch_step
WHEN (SELECT state FROM dispatch WHERE run_id = NEW.run_id) IS NOT 'draft'
BEGIN SELECT RAISE(ABORT, 'the plan is frozen once the dispatch leaves draft'); END;
CREATE TRIGGER dispatch_step_no_update BEFORE UPDATE ON dispatch_step
BEGIN SELECT RAISE(ABORT, 'plan steps are written once'); END;
CREATE TRIGGER dispatch_step_no_delete BEFORE DELETE ON dispatch_step
BEGIN SELECT RAISE(ABORT, 'plan steps are written once'); END;

-- Review notes on any node (`root`, a ticket id, a step id). Append-only, only while draft; frozen into
-- dispatch.md, where they bind the executor.
CREATE TABLE dispatch_note (
  id      INTEGER PRIMARY KEY,
  run_id  TEXT NOT NULL REFERENCES dispatch(run_id),
  node_id TEXT NOT NULL,
  author  TEXT NOT NULL,
  body    TEXT NOT NULL CHECK (length(trim(body)) > 0),
  at      TEXT NOT NULL
);
CREATE TRIGGER dispatch_note_draft_only BEFORE INSERT ON dispatch_note
WHEN (SELECT state FROM dispatch WHERE run_id = NEW.run_id) IS NOT 'draft'
BEGIN SELECT RAISE(ABORT, 'notes close when the dispatch leaves draft'); END;
CREATE TRIGGER dispatch_note_append_only_u BEFORE UPDATE ON dispatch_note
BEGIN SELECT RAISE(ABORT, 'notes are append-only'); END;
CREATE TRIGGER dispatch_note_append_only_d BEFORE DELETE ON dispatch_note
BEGIN SELECT RAISE(ABORT, 'notes are append-only'); END;""",
    7: "ALTER TABLE dispatch_step ADD COLUMN parent TEXT;",
    # Flags become decisions: options with consequences and a recommendation. Held reconcile writes carry over
    # (answered ones keep the user's resolution as the note); Kanban-mirror failures are no longer asked.
    # Planned drafts get their review decision.
    8: """ALTER TABLE writeback ADD COLUMN approved_by TEXT;
DROP TRIGGER writeback_no_upgrade;
CREATE TRIGGER writeback_no_upgrade BEFORE UPDATE OF decision ON writeback
WHEN NEW.decision IS NOT OLD.decision AND NOT (OLD.decision = 'apply' AND NEW.decision = 'flag')
  AND NOT (OLD.decision = 'flag' AND NEW.decision = 'apply' AND OLD.approved_by IS NULL AND NEW.approved_by IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'writeback decision may only be downgraded apply -> flag, or re-applied by a person'); END;
CREATE TABLE decision (
  id           INTEGER PRIMARY KEY,
  run_id       TEXT,
  node_id      TEXT NOT NULL DEFAULT 'root',
  issue_id     TEXT,
  kind         TEXT NOT NULL CHECK (kind IN
    ('review', 'plan', 'blocked', 'executor-gone', 'dispatch-stuck', 'writeback', 'ask')),
  ref          TEXT,
  question     TEXT NOT NULL CHECK (length(trim(question)) > 0),
  options_json TEXT NOT NULL CHECK (json_valid(options_json) AND json_array_length(options_json) >= 2),
  recommended  TEXT NOT NULL,
  why          TEXT NOT NULL CHECK (length(trim(why)) > 0),
  detail_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
  created_at   TEXT NOT NULL,
  created_by   TEXT NOT NULL,
  chosen       TEXT,
  chosen_by    TEXT,
  chosen_at    TEXT,
  chosen_note  TEXT,
  void_reason  TEXT,
  void_at      TEXT,
  CHECK ((chosen IS NULL) = (chosen_by IS NULL) AND (chosen IS NULL) = (chosen_at IS NULL)),
  CHECK ((void_reason IS NULL) = (void_at IS NULL) AND (chosen IS NULL OR void_reason IS NULL))
);
CREATE INDEX decision_open ON decision(run_id) WHERE chosen IS NULL AND void_reason IS NULL;
CREATE TRIGGER decision_valid BEFORE INSERT ON decision
WHEN NEW.chosen IS NOT NULL OR NEW.void_reason IS NOT NULL
  OR EXISTS (SELECT 1 FROM json_each(NEW.options_json) WHERE json_type(value) <> 'object'
             OR length(trim(coalesce(json_extract(value, '$.id'), ''))) = 0
             OR length(trim(coalesce(json_extract(value, '$.label'), ''))) = 0
             OR length(trim(coalesce(json_extract(value, '$.leads_to'), ''))) = 0)
  OR (SELECT count(DISTINCT json_extract(value, '$.id')) FROM json_each(NEW.options_json))
     <> json_array_length(NEW.options_json)
  OR NOT EXISTS (SELECT 1 FROM json_each(NEW.options_json) WHERE json_extract(value, '$.id') = NEW.recommended)
BEGIN SELECT RAISE(ABORT, 'a decision needs >= 2 distinct options (id, label, leads_to) and recommends one of them'); END;
CREATE TRIGGER decision_answer_once BEFORE UPDATE ON decision
WHEN OLD.chosen IS NOT NULL OR OLD.void_reason IS NOT NULL
  OR NEW.run_id IS NOT OLD.run_id OR NEW.node_id IS NOT OLD.node_id OR NEW.issue_id IS NOT OLD.issue_id
  OR NEW.kind IS NOT OLD.kind OR NEW.ref IS NOT OLD.ref OR NEW.question IS NOT OLD.question
  OR NEW.options_json IS NOT OLD.options_json OR NEW.recommended IS NOT OLD.recommended OR NEW.why IS NOT OLD.why
  OR NEW.detail_json IS NOT OLD.detail_json OR NEW.created_at IS NOT OLD.created_at
  OR NEW.created_by IS NOT OLD.created_by
  OR (NEW.chosen IS NULL AND NEW.void_reason IS NULL)
  OR (NEW.chosen IS NOT NULL AND NOT EXISTS (SELECT 1 FROM json_each(OLD.options_json)
                                             WHERE json_extract(value, '$.id') = NEW.chosen))
  OR (NEW.chosen IS NOT NULL AND length(trim(coalesce(NEW.chosen_note, ''))) = 0
      AND EXISTS (SELECT 1 FROM json_each(OLD.options_json)
                  WHERE json_extract(value, '$.id') = NEW.chosen AND json_extract(value, '$.note') IS NOT NULL))
BEGIN SELECT RAISE(ABORT, 'a decision is answered (with one of its options, and the text it asks for) or withdrawn once'); END;
CREATE TRIGGER decision_no_delete BEFORE DELETE ON decision
BEGIN SELECT RAISE(ABORT, 'decisions are never deleted'); END;
INSERT INTO decision(run_id, node_id, issue_id, kind, ref, question, options_json, recommended, why, detail_json,
                     created_at, created_by)
SELECT f.run_id, coalesce(l.identifier, 'root'), f.issue_id, 'writeback', json_extract(f.detail_json, '$.op'),
       CASE json_extract(f.detail_json, '$.op')
         WHEN 'state' THEN 'Move ' || coalesce(l.identifier, 'the ticket') || ' to '
                           || coalesce(json_extract(f.detail_json, '$.state'), 'its new state') || ' in Linear?'
         WHEN 'description' THEN 'Add the Completion block to ' || coalesce(l.identifier, 'the ticket') || '?'
         WHEN 'create' THEN 'Create the follow-up ticket from ' || coalesce(l.identifier, 'the ticket') || '?'
         ELSE 'Post the comment on ' || coalesce(l.identifier, 'the ticket') || '?' END,
       CASE WHEN json_extract(f.detail_json, '$.reason') LIKE 'reconcile agent:%' THEN
         '[{"id":"apply","label":"Apply anyway","leads_to":"reconcile sends it on its next run; the assignee and ticket-changed checks still apply"},'
         || '{"id":"skip","label":"Skip it","leads_to":"nothing is written to Linear"},'
         || '{"id":"manual","label":"I''ll do it in Linear","leads_to":"the factory writes nothing; you change the ticket yourself"}]'
       ELSE
         '[{"id":"skip","label":"Skip it","leads_to":"nothing is written to Linear"},'
         || '{"id":"manual","label":"I''ll do it in Linear","leads_to":"the factory writes nothing; you change the ticket yourself"}]'
       END,
       'skip', coalesce(json_extract(f.detail_json, '$.reason'), 'held by reconcile'), f.detail_json,
       f.created_at, 'factory:reconcile'
FROM flag f LEFT JOIN linear_latest l USING (issue_id) WHERE f.kind LIKE 'reconcile:%' ORDER BY f.id;
UPDATE decision SET chosen = 'skip', chosen_by = 'user (flag resolution)', chosen_note = f.resolution,
       chosen_at = f.resolved_at
FROM flag f WHERE f.resolved_at IS NOT NULL AND f.kind LIKE 'reconcile:%' AND f.run_id IS decision.run_id
  AND f.issue_id IS decision.issue_id AND f.created_at = decision.created_at
  AND json_extract(f.detail_json, '$.op') IS decision.ref;
DROP TABLE flag;
INSERT INTO decision(run_id, kind, question, options_json, recommended, why, created_at, created_by)
SELECT run_id, 'review', 'Start this dispatch?',
       CASE WHEN held_reason IS NULL THEN
         '[{"id":"approve","label":"Approve & start","leads_to":"the plan, notes and answers freeze; factory-fleet builds it and opens PRs; reconcile then writes the results to Linear"},'
         || '{"id":"hold","label":"Hold","leads_to":"no automatic start; it waits until you approve or reject","note":"Why hold it?"},'
         || '{"id":"reject","label":"Reject","leads_to":"the draft is archived; its tickets are not drafted again until they change","note":"Why reject it?"}]'
       ELSE
         '[{"id":"approve","label":"Approve & start","leads_to":"the plan, notes and answers freeze; factory-fleet builds it and opens PRs; reconcile then writes the results to Linear"},'
         || '{"id":"reject","label":"Reject","leads_to":"the draft is archived; its tickets are not drafted again until they change","note":"Why reject it?"}]'
       END,
       'approve', 'The plan is written and nothing has changed since it was drafted.', planned_at, 'factory:migration'
FROM dispatch WHERE state = 'draft' AND planned_at IS NOT NULL;""",
    # v9: less asking. Each decision gets a tier (auto / digest / now) and its own clock (notified_at, due_at),
    # which replaces the dispatch's review window; the notice table records pushes and digests.
    9: """DROP TRIGGER decision_answer_once;
ALTER TABLE decision ADD COLUMN tier TEXT NOT NULL DEFAULT 'digest' CHECK (tier IN ('auto', 'digest', 'now'));
ALTER TABLE decision ADD COLUMN notified_at TEXT;
ALTER TABLE decision ADD COLUMN due_at TEXT;
UPDATE decision SET tier = 'now' WHERE kind IN ('ask', 'executor-gone');
UPDATE decision SET notified_at = d.notified_at, due_at = d.review_until FROM dispatch d
WHERE decision.run_id = d.run_id AND decision.kind = 'review' AND decision.chosen IS NULL
  AND decision.void_reason IS NULL AND d.review_until IS NOT NULL;
ALTER TABLE dispatch DROP COLUMN notified_at;
ALTER TABLE dispatch DROP COLUMN review_until;
CREATE TRIGGER decision_answer_once BEFORE UPDATE OF run_id, node_id, issue_id, kind, ref, question, options_json,
  recommended, why, detail_json, created_at, created_by, tier, chosen, chosen_by, chosen_at, chosen_note, void_reason,
  void_at ON decision
WHEN OLD.chosen IS NOT NULL OR OLD.void_reason IS NOT NULL
  OR NEW.run_id IS NOT OLD.run_id OR NEW.node_id IS NOT OLD.node_id OR NEW.issue_id IS NOT OLD.issue_id
  OR NEW.kind IS NOT OLD.kind OR NEW.ref IS NOT OLD.ref OR NEW.question IS NOT OLD.question
  OR NEW.options_json IS NOT OLD.options_json OR NEW.recommended IS NOT OLD.recommended OR NEW.why IS NOT OLD.why
  OR NEW.detail_json IS NOT OLD.detail_json OR NEW.created_at IS NOT OLD.created_at
  OR NEW.created_by IS NOT OLD.created_by OR NEW.tier IS NOT OLD.tier
  OR (NEW.chosen IS NULL AND NEW.void_reason IS NULL)
  OR (NEW.chosen IS NOT NULL AND NOT EXISTS (SELECT 1 FROM json_each(OLD.options_json)
                                             WHERE json_extract(value, '$.id') = NEW.chosen))
  OR (NEW.chosen IS NOT NULL AND length(trim(coalesce(NEW.chosen_note, ''))) = 0
      AND EXISTS (SELECT 1 FROM json_each(OLD.options_json)
                  WHERE json_extract(value, '$.id') = NEW.chosen AND json_extract(value, '$.note') IS NOT NULL))
BEGIN SELECT RAISE(ABORT, 'a decision is answered (with one of its options, and the text it asks for) or withdrawn once'); END;
CREATE TRIGGER decision_clock BEFORE UPDATE OF notified_at, due_at ON decision
WHEN OLD.chosen IS NOT NULL OR OLD.void_reason IS NOT NULL
  OR (OLD.notified_at IS NOT NULL AND NEW.notified_at IS NOT OLD.notified_at)
  OR (OLD.due_at IS NOT NULL AND NEW.due_at IS NOT OLD.due_at)
BEGIN SELECT RAISE(ABORT, 'a decision''s clock (notified_at, due_at) is set once, while it is open'); END;
CREATE TABLE notice (
  id   INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('push', 'digest')),
  slot TEXT UNIQUE,
  body TEXT NOT NULL,
  at   TEXT NOT NULL
);""",
    # v10: history tables are append-only against raw sqlite3 too; a draft leaves only via approve/reject; a
    # confirmed write-back is final unless a person applies it anyway (approved_by set).
    10: """CREATE TRIGGER linear_snapshot_no_delete BEFORE DELETE ON linear_snapshot
BEGIN SELECT RAISE(ABORT, 'linear_snapshot is append-only'); END;
CREATE TRIGGER witness_log_no_delete BEFORE DELETE ON witness_log
BEGIN SELECT RAISE(ABORT, 'witness_log is append-only'); END;
CREATE TRIGGER verdict_no_delete BEFORE DELETE ON verdict
BEGIN SELECT RAISE(ABORT, 'verdicts are never deleted'); END;
CREATE TRIGGER card_event_no_delete BEFORE DELETE ON card_event
BEGIN SELECT RAISE(ABORT, 'card_event is append-only'); END;
DROP TRIGGER dispatch_no_delete;
CREATE TRIGGER dispatch_no_delete BEFORE DELETE ON dispatch
BEGIN SELECT RAISE(ABORT, 'dispatches are never deleted; a draft leaves approved or rejected'); END;
CREATE TRIGGER writeback_confirmed_final BEFORE UPDATE OF status ON writeback
WHEN OLD.status = 'confirmed' AND NEW.status IS NOT OLD.status
  AND NOT (OLD.approved_by IS NULL AND NEW.approved_by IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'a confirmed write-back is final unless a person applies it anyway'); END;""",
    # v11: cost ledger, derived from the agents' own session records (factory costs.sync); rewritable.
    11: """CREATE TABLE cost_session (
  id TEXT PRIMARY KEY, stage TEXT NOT NULL, run_id TEXT, started_at TEXT NOT NULL, mtime REAL NOT NULL,
  input INTEGER NOT NULL, output INTEGER NOT NULL, cache_read INTEGER NOT NULL, usd REAL NOT NULL
);""",
    # v12: the factory-fleet home that runs a dispatch (factory.toml context route), frozen with the rest.
    12: """ALTER TABLE dispatch ADD COLUMN route TEXT;
DROP TRIGGER dispatch_frozen;
CREATE TRIGGER dispatch_frozen BEFORE UPDATE ON dispatch
WHEN OLD.state <> 'draft' AND (NEW.body_sha256 IS NOT OLD.body_sha256 OR NEW.repos_json IS NOT OLD.repos_json
  OR NEW.run_id IS NOT OLD.run_id OR NEW.created_at IS NOT OLD.created_at OR NEW.route IS NOT OLD.route)
BEGIN SELECT RAISE(ABORT, 'dispatch is immutable once staged'); END;""",
    # v13: what the configurator shows per choice: a node's predicted result, the files a step touches (NULL on
    # older plans).
    13: """ALTER TABLE dispatch_step ADD COLUMN result TEXT;
ALTER TABLE dispatch_step ADD COLUMN files_json TEXT CHECK (files_json IS NULL OR json_valid(files_json));""",
    # v14: learnings (learn.py), and decision kind 'learning' (keep/drop a proposed one). SQLite can't alter a
    # CHECK: the decision table is rebuilt as-is with the new kind, its index and triggers recreated.
    14: """CREATE TABLE learning (
  id             INTEGER PRIMARY KEY,
  kind           TEXT NOT NULL CHECK (kind IN ('codemap', 'pitfall', 'house_rule')),
  scope          TEXT NOT NULL,
  body           TEXT NOT NULL CHECK (length(trim(body)) > 0 AND length(body) <= 300),
  anchors_json   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(anchors_json)),
  trunk_sha      TEXT,
  source         TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('active', 'proposed', 'expired', 'rejected')),
  created_at     TEXT NOT NULL,
  expired_reason TEXT,
  uses           INTEGER NOT NULL DEFAULT 0,
  UNIQUE (kind, scope, source, anchors_json)
);
CREATE UNIQUE INDEX learning_codemap ON learning(scope, anchors_json) WHERE kind = 'codemap' AND status = 'active';
CREATE TABLE decision_v14 (
  id           INTEGER PRIMARY KEY,
  run_id       TEXT,
  node_id      TEXT NOT NULL DEFAULT 'root',
  issue_id     TEXT,
  kind         TEXT NOT NULL CHECK (kind IN
    ('review', 'plan', 'blocked', 'executor-gone', 'dispatch-stuck', 'writeback', 'ask', 'learning')),
  ref          TEXT,
  question     TEXT NOT NULL CHECK (length(trim(question)) > 0),
  options_json TEXT NOT NULL CHECK (json_valid(options_json) AND json_array_length(options_json) >= 2),
  recommended  TEXT NOT NULL,
  why          TEXT NOT NULL CHECK (length(trim(why)) > 0),
  detail_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
  created_at   TEXT NOT NULL,
  created_by   TEXT NOT NULL,
  chosen       TEXT,
  chosen_by    TEXT,
  chosen_at    TEXT,
  chosen_note  TEXT,
  void_reason  TEXT,
  void_at      TEXT,
  tier         TEXT NOT NULL DEFAULT 'digest' CHECK (tier IN ('auto', 'digest', 'now')),
  notified_at  TEXT,
  due_at       TEXT,
  CHECK ((chosen IS NULL) = (chosen_by IS NULL) AND (chosen IS NULL) = (chosen_at IS NULL)),
  CHECK ((void_reason IS NULL) = (void_at IS NULL) AND (chosen IS NULL OR void_reason IS NULL))
);
INSERT INTO decision_v14(id, run_id, node_id, issue_id, kind, ref, question, options_json, recommended, why,
  detail_json, created_at, created_by, chosen, chosen_by, chosen_at, chosen_note, void_reason, void_at, tier,
  notified_at, due_at)
SELECT id, run_id, node_id, issue_id, kind, ref, question, options_json, recommended, why, detail_json, created_at,
  created_by, chosen, chosen_by, chosen_at, chosen_note, void_reason, void_at, tier, notified_at, due_at FROM decision;
DROP TABLE decision;
ALTER TABLE decision_v14 RENAME TO decision;
CREATE INDEX decision_open ON decision(run_id) WHERE chosen IS NULL AND void_reason IS NULL;
CREATE TRIGGER decision_valid BEFORE INSERT ON decision
WHEN NEW.chosen IS NOT NULL OR NEW.void_reason IS NOT NULL
  OR EXISTS (SELECT 1 FROM json_each(NEW.options_json) WHERE json_type(value) <> 'object'
             OR length(trim(coalesce(json_extract(value, '$.id'), ''))) = 0
             OR length(trim(coalesce(json_extract(value, '$.label'), ''))) = 0
             OR length(trim(coalesce(json_extract(value, '$.leads_to'), ''))) = 0)
  OR (SELECT count(DISTINCT json_extract(value, '$.id')) FROM json_each(NEW.options_json))
     <> json_array_length(NEW.options_json)
  OR NOT EXISTS (SELECT 1 FROM json_each(NEW.options_json) WHERE json_extract(value, '$.id') = NEW.recommended)
BEGIN SELECT RAISE(ABORT, 'a decision needs >= 2 distinct options (id, label, leads_to) and recommends one of them'); END;
CREATE TRIGGER decision_answer_once BEFORE UPDATE OF run_id, node_id, issue_id, kind, ref, question, options_json,
  recommended, why, detail_json, created_at, created_by, tier, chosen, chosen_by, chosen_at, chosen_note, void_reason,
  void_at ON decision
WHEN OLD.chosen IS NOT NULL OR OLD.void_reason IS NOT NULL
  OR NEW.run_id IS NOT OLD.run_id OR NEW.node_id IS NOT OLD.node_id OR NEW.issue_id IS NOT OLD.issue_id
  OR NEW.kind IS NOT OLD.kind OR NEW.ref IS NOT OLD.ref OR NEW.question IS NOT OLD.question
  OR NEW.options_json IS NOT OLD.options_json OR NEW.recommended IS NOT OLD.recommended OR NEW.why IS NOT OLD.why
  OR NEW.detail_json IS NOT OLD.detail_json OR NEW.created_at IS NOT OLD.created_at
  OR NEW.created_by IS NOT OLD.created_by OR NEW.tier IS NOT OLD.tier
  OR (NEW.chosen IS NULL AND NEW.void_reason IS NULL)
  OR (NEW.chosen IS NOT NULL AND NOT EXISTS (SELECT 1 FROM json_each(OLD.options_json)
                                             WHERE json_extract(value, '$.id') = NEW.chosen))
  OR (NEW.chosen IS NOT NULL AND length(trim(coalesce(NEW.chosen_note, ''))) = 0
      AND EXISTS (SELECT 1 FROM json_each(OLD.options_json)
                  WHERE json_extract(value, '$.id') = NEW.chosen AND json_extract(value, '$.note') IS NOT NULL))
BEGIN SELECT RAISE(ABORT, 'a decision is answered (with one of its options, and the text it asks for) or withdrawn once'); END;
CREATE TRIGGER decision_no_delete BEFORE DELETE ON decision
BEGIN SELECT RAISE(ABORT, 'decisions are never deleted'); END;
CREATE TRIGGER decision_clock BEFORE UPDATE OF notified_at, due_at ON decision
WHEN OLD.chosen IS NOT NULL OR OLD.void_reason IS NOT NULL
  OR (OLD.notified_at IS NOT NULL AND NEW.notified_at IS NOT OLD.notified_at)
  OR (OLD.due_at IS NOT NULL AND NEW.due_at IS NOT OLD.due_at)
BEGIN SELECT RAISE(ABORT, 'a decision''s clock (notified_at, due_at) is set once, while it is open'); END;""",
    # v15: "why?" threads on decisions (factory ask).
    15: """CREATE TABLE ask (
  id INTEGER PRIMARY KEY, decision_id INTEGER NOT NULL REFERENCES decision(id),
  question TEXT NOT NULL CHECK (length(trim(question)) > 0), answer TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'answered', 'failed')),
  session_id TEXT, asked_by TEXT NOT NULL, asked_at TEXT NOT NULL, answered_at TEXT, error TEXT,
  CHECK ((status = 'answered') = (answer IS NOT NULL) AND (status = 'failed') = (error IS NOT NULL)));
CREATE UNIQUE INDEX ask_one_pending ON ask(decision_id) WHERE status = 'pending';""",
    # v16: replan. A draft's plan steps may be cleared (only while draft) so the planner writes a new plan.
    16: """DROP TRIGGER dispatch_step_no_delete;
CREATE TRIGGER dispatch_step_no_delete BEFORE DELETE ON dispatch_step
WHEN (SELECT state FROM dispatch WHERE run_id = OLD.run_id) IS NOT 'draft'
BEGIN SELECT RAISE(ABORT, 'the plan is frozen once the dispatch leaves draft'); END;""",
    # v17: answering an executor question and queuing its exact message commit together. Old sends are unknown.
    17: """CREATE TABLE executor_delivery (
  decision_id INTEGER PRIMARY KEY REFERENCES decision(id),
  answer TEXT NOT NULL,
  message TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending', 'sending', 'sent', 'failed')),
  error TEXT,
  attempted_at TEXT,
  sent_at TEXT,
  sender_pid INTEGER CHECK (sender_pid > 0),
  CHECK ((state = 'sending') = (sender_pid IS NOT NULL))
);
CREATE TRIGGER executor_delivery_frozen BEFORE UPDATE OF decision_id, answer, message ON executor_delivery
WHEN NEW.decision_id IS NOT OLD.decision_id OR NEW.answer IS NOT OLD.answer OR NEW.message IS NOT OLD.message
BEGIN SELECT RAISE(ABORT, 'the recorded executor answer is immutable'); END;
INSERT INTO executor_delivery(decision_id, answer, message, state, error)
SELECT d.id, json_extract(o.value, '$.label'),
  'Answer to factory decision #' || d.id || ' (' || d.question || '): ' || json_extract(o.value, '$.label') || '.'
    || CASE WHEN d.chosen_note IS NOT NULL THEN ' Note: ' || d.chosen_note ELSE '' END
    || ' (by ' || d.chosen_by || '; also in `factory decide list ' || d.run_id || ' --all`)',
  'failed', 'Delivery predates durable tracking; it may already have arrived. Resending may duplicate it.'
FROM decision d JOIN dispatch r ON r.run_id=d.run_id, json_each(d.options_json) o
WHERE d.kind='ask' AND d.chosen IS NOT NULL AND r.state='executing'
  AND json_extract(o.value, '$.id')=d.chosen;""",
    # v18: only acknowledged Bot Chat delivery starts silence; old open clocks have no receipt.
    18: """ALTER TABLE notice ADD COLUMN execution_id TEXT;
ALTER TABLE notice ADD COLUMN decision_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(decision_ids_json));
ALTER TABLE notice ADD COLUMN delivered_at TEXT;
DROP TRIGGER decision_clock;
UPDATE decision SET notified_at=NULL, due_at=NULL
WHERE chosen IS NULL AND void_reason IS NULL AND tier <> 'auto';
CREATE TRIGGER decision_clock BEFORE UPDATE OF notified_at, due_at ON decision
WHEN OLD.chosen IS NOT NULL OR OLD.void_reason IS NOT NULL
  OR (OLD.notified_at IS NOT NULL AND NEW.notified_at IS NOT OLD.notified_at)
  OR (OLD.due_at IS NOT NULL AND NEW.due_at IS NOT OLD.due_at)
BEGIN SELECT RAISE(ABORT, 'a decision''s clock (notified_at, due_at) is set once, while it is open'); END;""",
    # v19: Jev guidance (and the learning slice's relation/group metadata) is persisted in
    # decision.detail_json.jev while a decision is open. detail_json stays frozen once answered or withdrawn,
    # like everything else on the row.
    19: """DROP TRIGGER decision_answer_once;
CREATE TRIGGER decision_answer_once BEFORE UPDATE OF run_id, node_id, issue_id, kind, ref, question, options_json,
  recommended, why, detail_json, created_at, created_by, tier, chosen, chosen_by, chosen_at, chosen_note, void_reason,
  void_at ON decision
WHEN OLD.chosen IS NOT NULL OR OLD.void_reason IS NOT NULL
  OR NEW.run_id IS NOT OLD.run_id OR NEW.node_id IS NOT OLD.node_id OR NEW.issue_id IS NOT OLD.issue_id
  OR NEW.kind IS NOT OLD.kind OR NEW.ref IS NOT OLD.ref OR NEW.question IS NOT OLD.question
  OR NEW.options_json IS NOT OLD.options_json OR NEW.recommended IS NOT OLD.recommended OR NEW.why IS NOT OLD.why
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.created_by IS NOT OLD.created_by OR NEW.tier IS NOT OLD.tier
  OR (NEW.chosen IS NULL AND NEW.void_reason IS NULL AND NEW.detail_json IS OLD.detail_json)
  OR (NEW.chosen IS NOT NULL AND NOT EXISTS (SELECT 1 FROM json_each(OLD.options_json)
                                             WHERE json_extract(value, '$.id') = NEW.chosen))
  OR (NEW.chosen IS NOT NULL AND length(trim(coalesce(NEW.chosen_note, ''))) = 0
      AND EXISTS (SELECT 1 FROM json_each(OLD.options_json)
                  WHERE json_extract(value, '$.id') = NEW.chosen AND json_extract(value, '$.note') IS NOT NULL))
BEGIN SELECT RAISE(ABORT, 'a decision is answered (with one of its options, and the text it asks for) or withdrawn once'); END;""",
}


def backup(conn: sqlite3.Connection, dest_dir: Path, tag: str, keep: int = 14) -> dict:
    """Online, consistent copy (sqlite backup API) to dest_dir/factory-<tag>.db, integrity-checked. Keeps the
    newest `keep` dated backups; tagged ones (pre-migration) are never pruned."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"factory-{tag}.db"
    tmp = dest.with_suffix(".tmp")
    with sqlite3.connect(tmp) as out:
        conn.backup(out)
        ok = out.execute("PRAGMA integrity_check").fetchone()[0]
    out.close()
    if ok != "ok":
        tmp.unlink()
        raise RuntimeError(f"backup integrity_check failed: {ok}")
    tmp.replace(dest)
    dated = sorted(dest_dir.glob("factory-20??-??-??.db"))
    for old in dated[:-keep]:
        old.unlink()
    return {"path": str(dest), "bytes": dest.stat().st_size, "kept": [p.name for p in dated[-keep:]]}


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript("BEGIN;\n" + files("factory").joinpath("schema.sql").read_text()
                           + f"\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;")
    elif version < SCHEMA_VERSION:
        backup(conn, path.parent / "factory" / "backups", f"pre-v{SCHEMA_VERSION}")
        for v in range(version + 1, SCHEMA_VERSION + 1):
            conn.executescript(f"BEGIN;\n{MIGRATIONS[v]}\nPRAGMA user_version={v};\nCOMMIT;")
    elif version > SCHEMA_VERSION:
        raise RuntimeError(f"factory.db schema v{version} is newer than code v{SCHEMA_VERSION}")
    return conn


class tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK (no-op when a script manages its own)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
            self.owned = True
        else:
            self.owned = False
        return self.conn

    def __exit__(self, exc_type, *_):
        if self.owned and self.conn.in_transaction:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
