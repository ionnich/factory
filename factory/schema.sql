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
  CHECK (state = 'draft' OR body_sha256 IS NOT NULL)
);
CREATE UNIQUE INDEX one_executing ON dispatch(state) WHERE state = 'executing';

CREATE TRIGGER dispatch_born_draft BEFORE INSERT ON dispatch WHEN NEW.state <> 'draft'
BEGIN SELECT RAISE(ABORT, 'dispatch must be created in draft'); END;

CREATE TRIGGER dispatch_edges BEFORE UPDATE OF state ON dispatch
WHEN NEW.state IS NOT OLD.state AND (OLD.state, NEW.state) NOT IN (VALUES
  ('draft', 'staged'), ('staged', 'executing'), ('executing', 'done'),
  ('done', 'reconciled'), ('reconciled', 'archived'))
BEGIN SELECT RAISE(ABORT, 'illegal dispatch transition'); END;

CREATE TRIGGER dispatch_frozen BEFORE UPDATE ON dispatch
WHEN OLD.state <> 'draft' AND (NEW.body_sha256 IS NOT OLD.body_sha256 OR NEW.repos_json IS NOT OLD.repos_json
  OR NEW.run_id IS NOT OLD.run_id OR NEW.created_at IS NOT OLD.created_at)
BEGIN SELECT RAISE(ABORT, 'dispatch is immutable once staged'); END;

CREATE TRIGGER dispatch_no_delete BEFORE DELETE ON dispatch WHEN OLD.state <> 'draft'
BEGIN SELECT RAISE(ABORT, 'only draft dispatches may be deleted'); END;

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

-- ---------------------------------------------------------------- reconcile
CREATE TABLE writeback (
  run_id       TEXT NOT NULL,
  issue_id     TEXT NOT NULL,
  op           TEXT NOT NULL CHECK (op IN ('state', 'comment', 'label')),
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  decision     TEXT NOT NULL CHECK (decision IN ('apply', 'skip', 'flag')),
  rule         TEXT NOT NULL,
  reason       TEXT,
  status       TEXT NOT NULL CHECK (status IN ('planned', 'sent', 'confirmed', 'failed')),
  linear_ref   TEXT,
  PRIMARY KEY (run_id, issue_id, op)
);

CREATE TABLE flag (
  id          INTEGER PRIMARY KEY,
  run_id      TEXT,
  issue_id    TEXT,
  kind        TEXT NOT NULL,
  detail_json TEXT NOT NULL CHECK (json_valid(detail_json)),
  created_at  TEXT NOT NULL,
  resolved_at TEXT,
  resolution  TEXT
);
