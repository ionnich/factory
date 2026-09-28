"""SQLite connection and schema bootstrap."""
import sqlite3
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

SCHEMA_VERSION = 1


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
    elif version != SCHEMA_VERSION:
        raise RuntimeError(f"factory.db schema v{version}, code expects v{SCHEMA_VERSION}")
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
