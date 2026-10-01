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

-- ---------------------------------------------------------------- work briefs
-- Strategy-stage statement of intent, published by a human before Factory verifies and executes it. Each row is
-- one immutable VERSION; a draft is editable, approving freezes body and sources, and amending a published brief
-- appends a new draft revision (parent_id -> prior version, revision + 1). The current version of a lineage is the
-- row no other row points to as parent.
CREATE TABLE work_brief (
  id               INTEGER PRIMARY KEY,
  revision         INTEGER NOT NULL CHECK (revision >= 1),
  parent_id        INTEGER REFERENCES work_brief(id),
  state            TEXT NOT NULL CHECK (state IN ('draft', 'approved', 'held')),
  body_json        TEXT NOT NULL CHECK (json_valid(body_json)),
  sources_json     TEXT NOT NULL CHECK (json_valid(sources_json) AND json_array_length(sources_json) > 0),
  created_at       TEXT NOT NULL,
  created_by       TEXT NOT NULL,
  approved_at      TEXT,
  approved_by      TEXT,
  amendment_reason TEXT,
  hold_reason      TEXT,
  CHECK ((approved_at IS NULL) = (approved_by IS NULL)),
  CHECK ((state IN ('approved', 'held')) = (approved_at IS NOT NULL))
);
CREATE INDEX work_brief_parent ON work_brief(parent_id) WHERE parent_id IS NOT NULL;
CREATE TRIGGER work_brief_root_revision BEFORE INSERT ON work_brief
WHEN NEW.parent_id IS NULL AND NEW.revision <> 1
BEGIN SELECT RAISE(ABORT, 'a root work brief is revision 1'); END;
CREATE TRIGGER work_brief_revision BEFORE INSERT ON work_brief
WHEN NEW.parent_id IS NOT NULL
 AND NEW.revision <> (SELECT revision + 1 FROM work_brief WHERE id = NEW.parent_id)
BEGIN SELECT RAISE(ABORT, 'an amendment increments its parent revision'); END;
CREATE TRIGGER work_brief_edges BEFORE UPDATE OF state ON work_brief
WHEN NEW.state IS NOT OLD.state AND (OLD.state, NEW.state) NOT IN (VALUES
  ('draft', 'approved'), ('approved', 'held'), ('held', 'approved'))
BEGIN SELECT RAISE(ABORT, 'illegal work_brief state transition'); END;
-- Published versions are immutable; amend (revise) to append a new draft revision instead of editing.
CREATE TRIGGER work_brief_frozen BEFORE UPDATE ON work_brief
WHEN OLD.state IN ('approved', 'held') AND (NEW.body_json IS NOT OLD.body_json
  OR NEW.sources_json IS NOT OLD.sources_json OR NEW.parent_id IS NOT OLD.parent_id
  OR NEW.revision IS NOT OLD.revision OR NEW.created_at IS NOT OLD.created_at
  OR NEW.created_by IS NOT OLD.created_by OR NEW.amendment_reason IS NOT OLD.amendment_reason)
BEGIN SELECT RAISE(ABORT, 'a published work brief is immutable; amend it to create a new revision'); END;
CREATE TRIGGER work_brief_no_delete BEFORE DELETE ON work_brief
BEGIN SELECT RAISE(ABORT, 'work briefs are never deleted'); END;

-- Hold/unhold audit: every explicit readiness change, append-only.
CREATE TABLE work_brief_hold (
  id       INTEGER PRIMARY KEY,
  brief_id INTEGER NOT NULL REFERENCES work_brief(id),
  action   TEXT NOT NULL CHECK (action IN ('hold', 'unhold')),
  reason   TEXT,
  actor    TEXT NOT NULL,
  at       TEXT NOT NULL
);
CREATE TRIGGER work_brief_hold_append_only_u BEFORE UPDATE ON work_brief_hold
BEGIN SELECT RAISE(ABORT, 'hold audit is append-only'); END;
CREATE TRIGGER work_brief_hold_append_only_d BEFORE DELETE ON work_brief_hold
BEGIN SELECT RAISE(ABORT, 'hold audit is append-only'); END;

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
  route         TEXT,                       -- factory-fleet home that runs it (context route); NULL = the captain routes
  brief_id      INTEGER REFERENCES work_brief(id),  -- the approved brief this dispatch pins (NULL = legacy path)
  -- review step (draft only): planner agent writes the plan, the user leaves notes, then approve/hold/reject. The
  -- review decision carries the clock (decision.due_at); a factory:propose draft may start without a person.
  drafted_by    TEXT,
  planned_at    TEXT, held_reason TEXT,
  -- plan step (draft only): when the plan gate last offered it to the planner, or a replan asked for a new plan (an
  -- offer, not proof a planner runs); why the planner's last plan was refused, cleared once a plan is written.
  planning_requested_at TEXT, planning_error TEXT,
  approved_by   TEXT, rejected_reason TEXT,
  emergency     INTEGER NOT NULL DEFAULT 0, -- 1 = no review window (urgent, one ticket, tiny evidence footprint)
  CHECK (state = 'draft' OR body_sha256 IS NOT NULL)  -- rejected drafts are rendered too, as the record
);
-- A brief version is dispatched at most once; NULL is the legacy (pre-Strategy) path.
CREATE UNIQUE INDEX dispatch_one_brief ON dispatch(brief_id) WHERE brief_id IS NOT NULL;

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
  OR NEW.run_id IS NOT OLD.run_id OR NEW.created_at IS NOT OLD.created_at OR NEW.route IS NOT OLD.route
  OR NEW.brief_id IS NOT OLD.brief_id)
BEGIN SELECT RAISE(ABORT, 'dispatch is immutable once staged'); END;

CREATE TRIGGER dispatch_no_delete BEFORE DELETE ON dispatch
BEGIN SELECT RAISE(ABORT, 'dispatches are never deleted; a draft leaves approved or rejected'); END;

-- Plan tree of a draft, written once by the planner agent, only while draft (a replan clears it for a new one). Rows: `root` (the dispatch's theme),
-- a ticket id (its role; `parent` = the ticket it is nested under, NULL = root), or a step (`FIN-1/2`, nested
-- `FIN-1/2.1`; parent derived from the id). depends_on = DAG edges across the dispatch. result = what a user or
-- system notices once the node lands; files_json = [{path, new}] a step touches (both NULL on pre-v13 plans).
CREATE TABLE dispatch_step (
  run_id          TEXT NOT NULL REFERENCES dispatch(run_id),
  step_id         TEXT NOT NULL,
  parent          TEXT,
  title           TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  detail          TEXT NOT NULL DEFAULT '',
  depends_on_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(depends_on_json)),
  result          TEXT,
  files_json      TEXT CHECK (files_json IS NULL OR json_valid(files_json)),
  PRIMARY KEY (run_id, step_id)
);
CREATE TRIGGER dispatch_step_draft_only BEFORE INSERT ON dispatch_step
WHEN (SELECT state FROM dispatch WHERE run_id = NEW.run_id) IS NOT 'draft'
BEGIN SELECT RAISE(ABORT, 'the plan is frozen once the dispatch leaves draft'); END;
CREATE TRIGGER dispatch_step_no_update BEFORE UPDATE ON dispatch_step
BEGIN SELECT RAISE(ABORT, 'plan steps are written once'); END;
CREATE TRIGGER dispatch_step_no_delete BEFORE DELETE ON dispatch_step
WHEN (SELECT state FROM dispatch WHERE run_id = OLD.run_id) IS NOT 'draft'
BEGIN SELECT RAISE(ABORT, 'the plan is frozen once the dispatch leaves draft'); END;

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
    ('review', 'plan', 'blocked', 'executor-gone', 'dispatch-stuck', 'writeback', 'ask', 'learning')),
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

-- Jev judgment guidance (and the learning slice's relation/group metadata) per decision: derived, replaceable
-- advice in its own row, never inside decision.detail_json (decision_answer_once keeps the decision immutable).
CREATE TABLE jev_advice (
  decision_id INTEGER PRIMARY KEY REFERENCES decision(id),
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
);
CREATE TRIGGER decision_clock BEFORE UPDATE OF notified_at, due_at ON decision
WHEN OLD.chosen IS NOT NULL OR OLD.void_reason IS NOT NULL
  OR (OLD.notified_at IS NOT NULL AND NEW.notified_at IS NOT OLD.notified_at)
  OR (OLD.due_at IS NOT NULL AND NEW.due_at IS NOT OLD.due_at)
BEGIN SELECT RAISE(ABORT, 'a decision''s clock (notified_at, due_at) is set once, while it is open'); END;

-- Exact recorded executor answer, queued with the choice; retries never choose again. Only explicit retries send.
CREATE TABLE executor_delivery (
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

-- Prepared pushes/digests, bound to the exact Hermes cron execution that carries stdout to Bot Chat.
-- delivered_at requires its successful delivery receipt; preparation alone never starts a decision clock.
CREATE TABLE notice (
  id   INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('push', 'digest')),
  slot TEXT UNIQUE,                          -- digest: the local 'YYYY-MM-DD HH:MM' it is for
  body TEXT NOT NULL,
  at   TEXT NOT NULL,
  execution_id TEXT,
  decision_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(decision_ids_json)),
  delivered_at TEXT
);

-- Cost ledger: each factory agent session (Hermes crons/chats, fleet captain/secondmates/crews) with its tokens
-- and cost, synced from the agents' own records by `costs.sync`. Derived data: rewritable, never a source of truth.
CREATE TABLE cost_session (
  id         TEXT PRIMARY KEY,               -- hermes:<profile>:<session id> | omp:<session file>
  stage      TEXT NOT NULL,                  -- prune | reconcile | plan | chat | captain | secondmate | crew
  run_id     TEXT,                           -- the dispatch it worked on (execution stages)
  started_at TEXT NOT NULL,
  mtime      REAL NOT NULL,                  -- source change marker: omp logs are re-read only when it moves
  input      INTEGER NOT NULL, output INTEGER NOT NULL, cache_read INTEGER NOT NULL,
  usd        REAL NOT NULL
);

-- Learnings: one- or two-line facts that save agents tokens, each with provenance (source), anchors (repo paths)
-- and expiry. codemap (what lives at a path) is harvested from verdict evidence and is active at
-- once; pitfall (from blocks) and house_rule (from the user's plan answers) are proposed and become active when the
-- user keeps them (decision kind 'learning', ref = id). Active ones expire when trunk changes an anchor. Derived by
-- `learn.sync`; uses counts `L<id>` cited in verdict reasons, plans and card comments.
CREATE TABLE learning (
  id             INTEGER PRIMARY KEY,
  kind           TEXT NOT NULL CHECK (kind IN ('codemap', 'pitfall', 'house_rule')),
  scope          TEXT NOT NULL,              -- repo OWNER/NAME
  body           TEXT NOT NULL CHECK (length(trim(body)) > 0 AND length(body) <= 300),
  anchors_json   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(anchors_json)),
  trunk_sha      TEXT,                       -- anchors checked at this trunk
  source         TEXT NOT NULL,              -- verdict:<id> | step:<run_id>:<step_id> | decision:<id> | theme:<label>
  status         TEXT NOT NULL CHECK (status IN ('active', 'proposed', 'expired', 'rejected')),
  created_at     TEXT NOT NULL,
  expired_reason TEXT,
  uses           INTEGER NOT NULL DEFAULT 0,
  UNIQUE (kind, scope, source, anchors_json)  -- harvested once, never again after it expires or is rejected
);
CREATE UNIQUE INDEX learning_codemap ON learning(scope, anchors_json) WHERE kind = 'codemap' AND status = 'active';

-- "Why?" on a decision: the planner explains inline (factory ask). One pending ask per decision; follow-ups
-- resume the Hermes session of the decision's last answer so the planner keeps context.
CREATE TABLE ask (
  id          INTEGER PRIMARY KEY,
  decision_id INTEGER NOT NULL REFERENCES decision(id),
  question    TEXT NOT NULL CHECK (length(trim(question)) > 0),
  answer      TEXT,
  status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'answered', 'failed')),
  session_id  TEXT,
  asked_by    TEXT NOT NULL,
  asked_at    TEXT NOT NULL,
  answered_at TEXT,
  error       TEXT,
  CHECK ((status = 'answered') = (answer IS NOT NULL) AND (status = 'failed') = (error IS NOT NULL))
);
CREATE UNIQUE INDEX ask_one_pending ON ask(decision_id) WHERE status = 'pending';

-- ---------------------------------------------------------------- brief-backed scheduling (v21)
-- Resource keys a dispatch claims for its whole run: repo:OWNER/NAME, route:home|<lead>, clickhouse:...,
-- stack:..., global:*. Pinned while the dispatch is a draft; immutable until it is archived (the claim is released
-- then). A key is <namespace>:<key>; conflicts are equal keys, same-namespace slash ancestor/descendant, or global:*.
CREATE TABLE dispatch_resource (
  run_id   TEXT NOT NULL REFERENCES dispatch(run_id),
  resource TEXT NOT NULL,
  PRIMARY KEY (run_id, resource)
);
CREATE TRIGGER dispatch_resource_draft_only_i BEFORE INSERT ON dispatch_resource
WHEN (SELECT state FROM dispatch WHERE run_id = NEW.run_id) IS NOT 'draft'
BEGIN SELECT RAISE(ABORT, 'dispatch resources are pinned while the dispatch is a draft'); END;
CREATE TRIGGER dispatch_resource_no_update BEFORE UPDATE ON dispatch_resource
BEGIN SELECT RAISE(ABORT, 'dispatch resources are immutable'); END;
CREATE TRIGGER dispatch_resource_held_to_archive BEFORE DELETE ON dispatch_resource
WHEN (SELECT state FROM dispatch WHERE run_id = OLD.run_id) IS NOT 'archived'
BEGIN SELECT RAISE(ABORT, 'dispatch resources are held until the dispatch is archived'); END;

-- The one scheduling policy, synced transactionally by the scheduler. Lowering max_parallel never kills work.
CREATE TABLE execution_policy (
  id           INTEGER PRIMARY KEY CHECK (id = 1),
  max_parallel INTEGER NOT NULL DEFAULT 2 CHECK (max_parallel >= 1)
);
INSERT INTO execution_policy(id, max_parallel) VALUES (1, 2);

-- A reserved launch slot: capacity + pane + route + resources are claimed atomically BEFORE the external /new or
-- intake send. reserved (not yet sent) -> sent (handed off) -> [terminal: deleted]; uncertain (send outcome
-- unknown) is shown to the operator and never auto-released (no TTL). Never /new twice after an uncertain send.
CREATE TABLE dispatch_launch (
  run_id     TEXT PRIMARY KEY REFERENCES dispatch(run_id),
  pane_id    TEXT NOT NULL CHECK (length(trim(pane_id)) > 0),
  state      TEXT NOT NULL CHECK (state IN ('reserved', 'sent', 'uncertain')),
  owner_pid  INTEGER,
  claimed_at TEXT,
  sent_at    TEXT,
  error      TEXT
);
CREATE TRIGGER launch_reserve_state BEFORE INSERT ON dispatch_launch
WHEN (SELECT state FROM dispatch WHERE run_id = NEW.run_id) IS NOT 'staged'
BEGIN SELECT RAISE(ABORT, 'a launch is reserved for a staged dispatch'); END;
CREATE TRIGGER launch_edges BEFORE UPDATE OF state ON dispatch_launch
WHEN NEW.state IS NOT OLD.state AND (OLD.state, NEW.state) NOT IN (VALUES
  ('reserved', 'sent'), ('reserved', 'uncertain'), ('uncertain', 'sent'), ('uncertain', 'reserved'))
BEGIN SELECT RAISE(ABORT, 'illegal launch state transition'); END;
-- Release a reservation only when it was definitely never sent, or once its dispatch is terminal (executor safe).
-- A sent/uncertain launch on a live dispatch cannot be silently dropped.
CREATE TRIGGER launch_release_guard BEFORE DELETE ON dispatch_launch
WHEN OLD.state IN ('sent', 'uncertain')
 AND (SELECT state FROM dispatch WHERE run_id = OLD.run_id) NOT IN ('done', 'reconciled', 'archived')
BEGIN SELECT RAISE(ABORT, 'a sent/uncertain launch is released only once its dispatch is terminal'); END;

-- Who holds a scheduling slot right now (capacity, route, pane): a running dispatch, or a non-terminal dispatch
-- with a reserved/sent/uncertain launch. Terminal dispatches free their slot.
CREATE VIEW launch_active AS
  SELECT run_id, route, executor_pane AS pane_id FROM dispatch WHERE state = 'executing'
  UNION ALL
  SELECT l.run_id, d.route, l.pane_id FROM dispatch_launch l JOIN dispatch d ON d.run_id = l.run_id
   WHERE l.state IN ('reserved', 'sent', 'uncertain')
     AND d.state NOT IN ('done', 'reconciled', 'archived');

-- Who holds resource claims right now: running + done + reconciled, plus non-terminal launches. Claims persist
-- through archive (they conflict with new work until then); only capacity/pane/route free at terminal.
CREATE VIEW resource_holders AS
  SELECT run_id FROM dispatch WHERE state IN ('executing', 'done', 'reconciled')
  UNION
  SELECT l.run_id FROM dispatch_launch l JOIN dispatch d ON d.run_id = l.run_id
   WHERE l.state IN ('reserved', 'sent', 'uncertain')
     AND d.state NOT IN ('done', 'reconciled', 'archived');

-- All conflicting resource pairs across every dispatch (equal key, same-namespace slash ancestor/descendant, or
-- any global namespace). namespace = text before the first ':', key = the rest.
CREATE VIEW resource_conflicts AS
SELECT c.run_id AS a, o.run_id AS b
FROM dispatch_resource c JOIN dispatch_resource o ON o.run_id <> c.run_id
WHERE c.resource = o.resource
   OR substr(c.resource, 1, instr(c.resource, ':') - 1) = 'global'
   OR substr(o.resource, 1, instr(o.resource, ':') - 1) = 'global'
   OR (substr(c.resource, 1, instr(c.resource, ':') - 1) = substr(o.resource, 1, instr(o.resource, ':') - 1)
       AND (substr(c.resource, instr(c.resource, ':') + 1) = substr(o.resource, instr(o.resource, ':') + 1)
            OR instr(substr(c.resource, instr(c.resource, ':') + 1), substr(o.resource, instr(o.resource, ':') + 1) || '/') = 1
            OR instr(substr(o.resource, instr(o.resource, ':') + 1), substr(c.resource, instr(c.resource, ':') + 1) || '/') = 1));

-- Atomic reserve: refuse when the cap (running + non-terminal launches), this pane, this route, or a claimed
-- resource would be shared. Global admission rules raw sqlite3 cannot bypass.
CREATE TRIGGER launch_reserve_guard BEFORE INSERT ON dispatch_launch
WHEN (SELECT count(DISTINCT run_id) FROM launch_active) + 1 > (SELECT max_parallel FROM execution_policy WHERE id = 1)
  OR EXISTS (SELECT 1 FROM launch_active WHERE pane_id = NEW.pane_id)
  OR ((SELECT route FROM dispatch WHERE run_id = NEW.run_id) IS NOT NULL
      AND EXISTS (SELECT 1 FROM launch_active WHERE route = (SELECT route FROM dispatch WHERE run_id = NEW.run_id)))
  OR EXISTS (SELECT 1 FROM resource_conflicts rc WHERE rc.a = NEW.run_id
             AND rc.b IN (SELECT run_id FROM resource_holders))
BEGIN SELECT RAISE(ABORT, 'launch conflicts with capacity, pane, route or a held resource'); END;

-- staged -> executing re-checks the same rules (a direct execute cannot bypass reserve), minus the self slot the
-- reservation already holds.
CREATE TRIGGER dispatch_execute_guard BEFORE UPDATE OF state ON dispatch
WHEN OLD.state = 'staged' AND NEW.state = 'executing' AND (
  (SELECT count(DISTINCT run_id) FROM launch_active WHERE run_id <> NEW.run_id) + 1
      > (SELECT max_parallel FROM execution_policy WHERE id = 1)
  OR EXISTS (SELECT 1 FROM launch_active WHERE pane_id = NEW.executor_pane AND run_id <> NEW.run_id)
  OR (NEW.route IS NOT NULL AND EXISTS (SELECT 1 FROM launch_active WHERE route = NEW.route AND run_id <> NEW.run_id))
  OR EXISTS (SELECT 1 FROM resource_conflicts rc WHERE rc.a = NEW.run_id
             AND rc.b IN (SELECT run_id FROM resource_holders WHERE run_id <> NEW.run_id)))
BEGIN SELECT RAISE(ABORT, 'execute conflicts with capacity, pane, route or a held resource'); END;

-- A brief-backed dispatch enters executing only from its own reserved/sent launch pane; legacy (NULL brief_id)
-- dispatches finish unchanged and are covered by dispatch_execute_guard alone.
CREATE TRIGGER dispatch_execute_launch BEFORE UPDATE OF state ON dispatch
WHEN OLD.state = 'staged' AND NEW.state = 'executing' AND NEW.brief_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM dispatch_launch WHERE run_id = NEW.run_id
                 AND state IN ('reserved', 'sent') AND pane_id = NEW.executor_pane)
BEGIN SELECT RAISE(ABORT, 'a brief-backed dispatch executes only from its reserved/sent launch pane'); END;
