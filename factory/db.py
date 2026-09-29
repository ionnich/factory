"""SQLite connection and schema bootstrap."""
import sqlite3
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

SCHEMA_VERSION = 5

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
}


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
