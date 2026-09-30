-- factory.db: the only authoritative tracker. Invariants live here as
-- constraints/triggers so raw sqlite3 writes cannot break them either.

-- ---------------------------------------------------------------- Linear
CREATE TABLE linear_snapshot (
  issue_id    TEXT NOT NULL,                 -- Linear UUID
  identifier  TEXT NOT NULL,                 -- e.g. FIN-123
  updated_at  TEXT NOT NULL,                 -- Linear updatedAt (ISO 8601 UTC)
  fetched_at  TEXT NOT NULL,
  state_type  TEXT NOT NULL,                 -- triage|backlog|unstarted|started|completed|canceled
  in_scope    INTEGER NOT NULL CHECK (in_scope IN (0, 1)),
  raw_json    TEXT NOT NULL CHECK (json_valid(raw_json)),
  PRIMARY KEY (issue_id, updated_at)
);
CREATE INDEX linear_snapshot_ident ON linear_snapshot(identifier, updated_at);
CREATE TRIGGER linear_snapshot_immutable BEFORE UPDATE ON linear_snapshot
BEGIN SELECT RAISE(ABORT, 'linear_snapshot is append-only'); END;
CREATE TRIGGER linear_snapshot_no_delete BEFORE DELETE ON linear_snapshot
BEGIN SELECT RAISE(ABORT, 'linear_snapshot is append-only'); END;

CREATE VIEW linear_latest AS
SELECT s.* FROM linear_snapshot s
WHERE s.updated_at = (SELECT max(updated_at) FROM linear_snapshot m WHERE m.issue_id = s.issue_id);

CREATE TABLE sync_cursor (
  name          TEXT PRIMARY KEY,
  updated_at_gt TEXT NOT NULL,
  last_run_at   TEXT NOT NULL,
  last_count    INTEGER NOT NULL
);

-- Linear projects = canonical Domain projects (finks-ddd). lead_email scopes what the factory owns.
CREATE TABLE linear_project (
  id         TEXT PRIMARY KEY,
  slug_id    TEXT NOT NULL UNIQUE,            -- trailing id in the project URL
  name       TEXT NOT NULL,
  lead_email TEXT,
  fetched_at TEXT NOT NULL
);

CREATE TABLE repo_trunk (
  repo       TEXT PRIMARY KEY,               -- OWNER/NAME
  branch     TEXT NOT NULL,
  sha        TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);

-- ---------------------------------------------------------------- witnesses
-- Every DB/API read made for evidence goes through `factory witness` and is
-- logged here; sql/dagster evidence must cite a row id.
CREATE TABLE witness_log (
  id            INTEGER PRIMARY KEY,
  witness       TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('clickhouse', 'dagster')),
  query         TEXT NOT NULL,
  ok            INTEGER NOT NULL CHECK (ok IN (0, 1)),
  rows          INTEGER,
  result_sha256 TEXT,
  result_excerpt TEXT,
  at            TEXT NOT NULL
);
CREATE TRIGGER witness_log_immutable BEFORE UPDATE ON witness_log
BEGIN SELECT RAISE(ABORT, 'witness_log is append-only'); END;
CREATE TRIGGER witness_log_no_delete BEFORE DELETE ON witness_log
BEGIN SELECT RAISE(ABORT, 'witness_log is append-only'); END;

-- ---------------------------------------------------------------- verdicts
CREATE TABLE verdict (
  id                  INTEGER PRIMARY KEY,
  issue_id            TEXT NOT NULL,
  snapshot_updated_at TEXT NOT NULL,
  context             TEXT,                  -- bounded context name; NULL = unmapped
  repo                TEXT,
  trunk_sha           TEXT,
  kind                TEXT NOT NULL CHECK (kind IN
    ('valid', 'already-done', 'stale', 'duplicate-of', 'invalid-references', 'needs-clarification')),
  target              TEXT CHECK ((kind IN ('duplicate-of', 'invalid-references')) = (target IS NOT NULL)),
  reason              TEXT NOT NULL CHECK (length(reason) > 0),
  evidence_json       TEXT NOT NULL CHECK (json_valid(evidence_json) AND json_array_length(evidence_json) > 0),
  evidence_paths_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(evidence_paths_json)),
  created_at          TEXT NOT NULL,
  created_by          TEXT NOT NULL,
  superseded_at       TEXT,
  written_back_run    TEXT,
  FOREIGN KEY (issue_id, snapshot_updated_at) REFERENCES linear_snapshot(issue_id, updated_at)
);
CREATE UNIQUE INDEX verdict_current ON verdict(issue_id) WHERE superseded_at IS NULL;
CREATE TRIGGER verdict_frozen BEFORE UPDATE ON verdict
WHEN NEW.issue_id IS NOT OLD.issue_id OR NEW.snapshot_updated_at IS NOT OLD.snapshot_updated_at
  OR NEW.context IS NOT OLD.context OR NEW.repo IS NOT OLD.repo OR NEW.trunk_sha IS NOT OLD.trunk_sha
  OR NEW.kind IS NOT OLD.kind OR NEW.target IS NOT OLD.target OR NEW.reason IS NOT OLD.reason
  OR NEW.evidence_json IS NOT OLD.evidence_json OR NEW.evidence_paths_json IS NOT OLD.evidence_paths_json
  OR (OLD.superseded_at IS NOT NULL AND NEW.superseded_at IS NOT OLD.superseded_at)
BEGIN SELECT RAISE(ABORT, 'verdict is immutable except superseded_at/written_back_run'); END;
CREATE TRIGGER verdict_no_delete BEFORE DELETE ON verdict
BEGIN SELECT RAISE(ABORT, 'verdicts are never deleted'); END;

-- ---------------------------------------------------------------- dispatches
-- Directory is derived: dispatches/<run_id> or dispatches/_archived/<run_id>.
CREATE TABLE dispatch (
  run_id        TEXT PRIMARY KEY,
  state         TEXT NOT NULL CHECK (state IN ('draft', 'staged', 'executing', 'done', 'reconciled', 'archived')),
  body_sha256   TEXT,
  repos_json    TEXT NOT NULL CHECK (json_valid(repos_json)),   -- [{repo, trunk_sha}]
  last_actor    TEXT NOT NULL,              -- who made the latest state change; copied to transition_log
  created_at    TEXT NOT NULL,
  staged_at     TEXT, executing_at TEXT, done_at TEXT, reconciled_at TEXT, archived_at TEXT,
  executor_pane TEXT,
  -- review step (draft only): planner agent writes the plan, the user leaves notes, then approve/hold/reject. The
  -- review decision carries the clock (decision.due_at); a factory:propose draft may start without a person.
  drafted_by    TEXT,
  planned_at    TEXT, held_reason TEXT,
  approved_by   TEXT, rejected_reason TEXT,
  emergency     INTEGER NOT NULL DEFAULT 0, -- 1 = no review window (urgent, one ticket, tiny evidence footprint)
  CHECK (state = 'draft' OR body_sha256 IS NOT NULL)  -- rejected drafts are rendered too, as the record
);
CREATE UNIQUE INDEX one_executing ON dispatch(state) WHERE state = 'executing';

CREATE TRIGGER dispatch_born_draft BEFORE INSERT ON dispatch WHEN NEW.state <> 'draft'
BEGIN SELECT RAISE(ABORT, 'dispatch must be created in draft'); END;

CREATE TRIGGER dispatch_edges BEFORE UPDATE OF state ON dispatch
WHEN NEW.state IS NOT OLD.state AND (OLD.state, NEW.state) NOT IN (VALUES
  ('draft', 'staged'), ('staged', 'executing'), ('executing', 'done'),
  ('done', 'reconciled'), ('reconciled', 'archived'), ('draft', 'archived'))
BEGIN SELECT RAISE(ABORT, 'illegal dispatch transition'); END;

-- Nothing leaves draft unreviewed: staging needs an approver, discarding needs a reason.
CREATE TRIGGER dispatch_review_gate BEFORE UPDATE OF state ON dispatch
WHEN OLD.state = 'draft' AND ((NEW.state = 'staged' AND NEW.approved_by IS NULL)
  OR (NEW.state = 'archived' AND NEW.rejected_reason IS NULL))
BEGIN SELECT RAISE(ABORT, 'a draft leaves review only approved (staged) or rejected with a reason'); END;

CREATE TRIGGER dispatch_frozen BEFORE UPDATE ON dispatch
WHEN OLD.state <> 'draft' AND (NEW.body_sha256 IS NOT OLD.body_sha256 OR NEW.repos_json IS NOT OLD.repos_json
  OR NEW.run_id IS NOT OLD.run_id OR NEW.created_at IS NOT OLD.created_at)
BEGIN SELECT RAISE(ABORT, 'dispatch is immutable once staged'); END;

CREATE TRIGGER dispatch_no_delete BEFORE DELETE ON dispatch
BEGIN SELECT RAISE(ABORT, 'dispatches are never deleted; a draft leaves approved or rejected'); END;

-- Plan tree of a draft, written once by the planner agent, only while draft. Rows: `root` (the dispatch's theme),
-- a ticket id (its role; `parent` = the ticket it is nested under, NULL = root), or a step (`FIN-1/2`, nested
-- `FIN-1/2.1`; parent derived from the id). depends_on = DAG edges across the dispatch.
CREATE TABLE dispatch_step (
  run_id          TEXT NOT NULL REFERENCES dispatch(run_id),
  step_id         TEXT NOT NULL,
  parent          TEXT,
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
BEGIN SELECT RAISE(ABORT, 'notes are append-only'); END;

CREATE TABLE transition_log (
  id         INTEGER PRIMARY KEY,
  run_id     TEXT NOT NULL,
  from_state TEXT,
  to_state   TEXT NOT NULL,
  actor      TEXT NOT NULL,
  at         TEXT NOT NULL
);
CREATE TRIGGER transition_log_append_only_u BEFORE UPDATE ON transition_log
BEGIN SELECT RAISE(ABORT, 'transition_log is append-only'); END;
CREATE TRIGGER transition_log_append_only_d BEFORE DELETE ON transition_log
BEGIN SELECT RAISE(ABORT, 'transition_log is append-only'); END;

CREATE TRIGGER dispatch_log_insert AFTER INSERT ON dispatch
BEGIN INSERT INTO transition_log(run_id, from_state, to_state, actor, at)
      VALUES (NEW.run_id, NULL, NEW.state, NEW.last_actor, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')); END;
CREATE TRIGGER dispatch_log_update AFTER UPDATE OF state ON dispatch WHEN NEW.state IS NOT OLD.state
BEGIN INSERT INTO transition_log(run_id, from_state, to_state, actor, at)
      VALUES (NEW.run_id, OLD.state, NEW.state, NEW.last_actor, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')); END;

-- One row per ticket in a dispatch. card_status here is authoritative;
-- the Hermes Kanban card (kanban_card_id) is an optional mirror.
CREATE TABLE dispatch_ticket (
  run_id              TEXT NOT NULL REFERENCES dispatch(run_id),
  issue_id            TEXT NOT NULL,
  identifier          TEXT NOT NULL,
  snapshot_updated_at TEXT NOT NULL,
  verdict_id          INTEGER NOT NULL REFERENCES verdict(id),
  card_status         TEXT NOT NULL DEFAULT 'ready' CHECK (card_status IN ('ready', 'running', 'done', 'blocked')),
  kanban_card_id      TEXT UNIQUE,
  pr_url              TEXT,
  PRIMARY KEY (run_id, issue_id),
  FOREIGN KEY (issue_id, snapshot_updated_at) REFERENCES linear_snapshot(issue_id, updated_at)
);

CREATE TRIGGER ticket_set_frozen_i BEFORE INSERT ON dispatch_ticket
WHEN (SELECT state FROM dispatch WHERE run_id = NEW.run_id) IS NOT 'draft'
BEGIN SELECT RAISE(ABORT, 'ticket set is frozen once staged'); END;
CREATE TRIGGER ticket_set_frozen_d BEFORE DELETE ON dispatch_ticket
WHEN (SELECT state FROM dispatch WHERE run_id = OLD.run_id) IS NOT 'draft'
BEGIN SELECT RAISE(ABORT, 'ticket set is frozen once staged'); END;
CREATE TRIGGER ticket_identity_frozen BEFORE UPDATE OF run_id, issue_id, identifier, snapshot_updated_at, verdict_id
ON dispatch_ticket WHEN (SELECT state FROM dispatch WHERE run_id = OLD.run_id) IS NOT 'draft'
BEGIN SELECT RAISE(ABORT, 'ticket set is frozen once staged'); END;
CREATE TRIGGER ticket_kanban_id_once BEFORE UPDATE OF kanban_card_id ON dispatch_ticket
WHEN OLD.kanban_card_id IS NOT NULL AND NEW.kanban_card_id IS NOT OLD.kanban_card_id
BEGIN SELECT RAISE(ABORT, 'kanban_card_id is set once'); END;

CREATE TRIGGER ticket_no_double_booking BEFORE INSERT ON dispatch_ticket
WHEN EXISTS (SELECT 1 FROM dispatch_ticket t JOIN dispatch d USING (run_id)
             WHERE t.issue_id = NEW.issue_id AND t.run_id <> NEW.run_id AND d.state <> 'archived')
BEGIN SELECT RAISE(ABORT, 'ticket already belongs to a live dispatch'); END;

-- Cards move only while their dispatch executes; done/blocked are terminal.
CREATE TRIGGER card_edges BEFORE UPDATE OF card_status ON dispatch_ticket
WHEN NEW.card_status IS NOT OLD.card_status AND (
  (SELECT state FROM dispatch WHERE run_id = OLD.run_id) IS NOT 'executing'
  OR (OLD.card_status, NEW.card_status) NOT IN (VALUES
       ('ready', 'running'), ('ready', 'done'), ('ready', 'blocked'),
       ('running', 'done'), ('running', 'blocked')))
BEGIN SELECT RAISE(ABORT, 'illegal card transition'); END;

-- executing -> done is derived: the last card reaching done|blocked closes the dispatch.
CREATE TRIGGER dispatch_auto_done AFTER UPDATE OF card_status ON dispatch_ticket
WHEN NEW.card_status IN ('done', 'blocked')
 AND NOT EXISTS (SELECT 1 FROM dispatch_ticket WHERE run_id = NEW.run_id AND card_status IN ('ready', 'running'))
BEGIN
  UPDATE dispatch SET state = 'done', done_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), last_actor = 'factory:all-cards-terminal'
  WHERE run_id = NEW.run_id AND state = 'executing';
END;

CREATE TABLE card_event (
  id            INTEGER PRIMARY KEY,
  run_id        TEXT NOT NULL,
  issue_id      TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('claim', 'comment', 'done', 'block')),
  actor         TEXT NOT NULL,
  body          TEXT,
  metadata_json TEXT CHECK (metadata_json IS NULL OR json_valid(metadata_json)),
  at            TEXT NOT NULL,
  FOREIGN KEY (run_id, issue_id) REFERENCES dispatch_ticket(run_id, issue_id)
);
CREATE TRIGGER card_event_append_only BEFORE UPDATE ON card_event
BEGIN SELECT RAISE(ABORT, 'card_event is append-only'); END;
CREATE TRIGGER card_event_no_delete BEFORE DELETE ON card_event
BEGIN SELECT RAISE(ABORT, 'card_event is append-only'); END;

-- ---------------------------------------------------------------- reconcile
-- One row per planned Linear write. Only `factory reconcile apply` sends; the reconcile agent may edit
-- comment/description prose and downgrade apply -> flag, nothing else. A held (flag) write goes back to apply
-- only when a person chose "apply anyway" on its decision (approved_by).
CREATE TABLE writeback (
  run_id       TEXT NOT NULL,               -- dispatch run_id, or sweep-<ts> for verdict write-backs
  issue_id     TEXT NOT NULL,
  op           TEXT NOT NULL CHECK (op IN ('state', 'comment', 'description', 'create')),  -- create: issue_id = parent
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  decision     TEXT NOT NULL CHECK (decision IN ('apply', 'skip', 'flag')),
  rule         TEXT NOT NULL,
  reason       TEXT,
  status       TEXT NOT NULL CHECK (status IN ('planned', 'sent', 'confirmed', 'failed')),
  linear_ref   TEXT,
  approved_by  TEXT,
  PRIMARY KEY (run_id, issue_id, op)
);
CREATE TRIGGER writeback_no_upgrade BEFORE UPDATE OF decision ON writeback
WHEN NEW.decision IS NOT OLD.decision AND NOT (OLD.decision = 'apply' AND NEW.decision = 'flag')
  AND NOT (OLD.decision = 'flag' AND NEW.decision = 'apply' AND OLD.approved_by IS NULL AND NEW.approved_by IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'writeback decision may only be downgraded apply -> flag, or re-applied by a person'); END;
CREATE TRIGGER writeback_confirmed_final BEFORE UPDATE OF status ON writeback
WHEN OLD.status = 'confirmed' AND NEW.status IS NOT OLD.status
  AND NOT (OLD.approved_by IS NULL AND NEW.approved_by IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'a confirmed write-back is final unless a person applies it anyway'); END;

-- updatedAt values produced by reconcile's own writes: not a ticket change, so no re-verification.
CREATE TABLE linear_own_write (
  issue_id   TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (issue_id, updated_at)
);

-- ---------------------------------------------------------------- decisions
-- Every choice the factory needs from a person: >= 2 options, each saying what it leads to, one recommended
-- with why. Answered once (chosen) or withdrawn once when the question stops applying (void); never edited.
-- node_id places it in its dispatch's graph (root, a ticket id, a step id); run_id may be a sweep-/followup- run.
-- tier, fixed when asked: auto = the factory takes the recommendation on its next pass (nothing for a person to
-- weigh, or the user took it the last EARNED_AFTER times); now = work is stopped until the user answers (pushed
-- at once); digest = it waits for the next digest. notified_at = when it first reached the user; due_at = when
-- silence takes the recommendation (NULL: it waits for the user). Both are set once, while it is open.
CREATE TABLE decision (
  id           INTEGER PRIMARY KEY,
  run_id       TEXT,
  node_id      TEXT NOT NULL DEFAULT 'root',
  issue_id     TEXT,
  kind         TEXT NOT NULL CHECK (kind IN
    ('review', 'plan', 'blocked', 'executor-gone', 'dispatch-stuck', 'writeback', 'ask')),
  ref          TEXT,                         -- kind-specific key, e.g. the writeback op
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
  chosen_note  TEXT,                         -- text an option asks for (a reason, guidance)
  void_reason  TEXT,
  void_at      TEXT,
  tier         TEXT NOT NULL DEFAULT 'digest' CHECK (tier IN ('auto', 'digest', 'now')),
  notified_at  TEXT,
  due_at       TEXT,
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
BEGIN SELECT RAISE(ABORT, 'a decision''s clock (notified_at, due_at) is set once, while it is open'); END;

-- What the factory told the user in the factory Bot Chat (Hermex): pushes when work is stopped on them (capped
-- per day) and the digest at notify.digest times (one per slot, recorded even when it had nothing to say).
CREATE TABLE notice (
  id   INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('push', 'digest')),
  slot TEXT UNIQUE,                          -- digest: the local 'YYYY-MM-DD HH:MM' it is for
  body TEXT NOT NULL,
  at   TEXT NOT NULL
);
