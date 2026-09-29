"""SQLite connection and schema bootstrap."""
import sqlite3
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

SCHEMA_VERSION = 7

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
