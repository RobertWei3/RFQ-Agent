"""SQLite connection helper and table definitions."""

from __future__ import annotations

import sqlite3
import threading
import time
import warnings
from pathlib import Path
from typing import Any

LLM_CALLS_DDL = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT    NOT NULL,
    rfq_run_id          TEXT,
    agent               TEXT,
    role                TEXT,
    provider            TEXT,
    model               TEXT,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cached_input_tokens INTEGER,
    cost_usd            REAL,
    latency_ms          REAL,
    status              TEXT    NOT NULL,
    error               TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls (rfq_run_id);
"""

SCHEMA_DDL = (LLM_CALLS_DDL,)

BUSY_TIMEOUT_MS = 30_000  # how long a writer waits for another process's lock

# SQLite 3.51.0 / 3.51.1 can deadlock inside the unix VFS when one thread opens a database
# file while another closes a connection to it (fixed in 3.51.2). Connections made through
# ``connect`` / ``open_sqlite`` (result store, LLM tracking, checkpoints) open and close
# under this lock. Not covered: the MCP server and the data generator, which open their
# own SQLite files with ``sqlite3.connect``, and connections closed by garbage collection
# instead of ``close()``. Use SQLite >= 3.51.2 (spec 18).
OPEN_CLOSE_LOCK = threading.Lock()
DEADLOCKING_SQLITE = ((3, 51, 0), (3, 51, 2))  # [from, to): affected releases


def check_sqlite_version(version: tuple[int, int, int]) -> None:
    """Warn when the linked SQLite is a release known to deadlock (see OPEN_CLOSE_LOCK)."""
    start, fixed = DEADLOCKING_SQLITE
    if start <= version < fixed:
        warnings.warn(
            f"SQLite {'.'.join(map(str, version))} can deadlock when threads open and close "
            "connections; upgrade to 3.51.2 or later",
            RuntimeWarning,
            stacklevel=2,
        )


class Connection(sqlite3.Connection):
    """A connection whose open (see ``open_sqlite``) and close never overlap another's."""

    def close(self) -> None:
        with OPEN_CLOSE_LOCK:
            super().close()


def open_sqlite(path: Path, **kwargs: Any) -> Connection:
    """``sqlite3.connect`` under ``OPEN_CLOSE_LOCK``, returning a ``Connection``."""
    with OPEN_CLOSE_LOCK:
        return sqlite3.connect(path, factory=Connection, **kwargs)


def connect(
    db_path: Path, *, shared: bool = False, busy_timeout_ms: int = BUSY_TIMEOUT_MS
) -> sqlite3.Connection:
    """Open ``db_path`` (creating parent dirs and tables if needed).

    ``shared``: the caller serializes use of the connection across threads itself.
    ``busy_timeout_ms``: how long a statement waits for another connection's lock.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_sqlite(db_path, timeout=busy_timeout_ms / 1000, check_same_thread=not shared)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    _use_wal(conn)
    for ddl in SCHEMA_DDL:
        conn.executescript(ddl)
    return conn


def _use_wal(conn: sqlite3.Connection) -> None:
    """Switch the file to WAL: several processes (CLI, API, workers) share it (spec 13).

    WAL is a property of the file, so this happens once. The switch takes an exclusive
    lock without waiting for the busy timeout, so a racing first open retries here.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.01)


check_sqlite_version(sqlite3.sqlite_version_info)
