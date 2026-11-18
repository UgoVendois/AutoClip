"""Base SQLite : schéma, connexion et transactions.

Toutes les dates sont des timestamps Unix (REAL) pour simplifier les calculs
et permettre une horloge injectable dans les tests.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS bot (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    clip_settings TEXT NOT NULL DEFAULT '{}',
    dry_run       INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS source (
    id         INTEGER PRIMARY KEY,
    bot_id     INTEGER NOT NULL REFERENCES bot(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('url', 'file')),
    uri        TEXT NOT NULL,
    media_path TEXT,
    sha256     TEXT,
    status     TEXT NOT NULL DEFAULT 'ingest'
               CHECK (status IN ('ingest', 'transcribe', 'analyze', 'done', 'failed')),
    error      TEXT,
    created_at REAL NOT NULL,
    UNIQUE (bot_id, uri)
);

-- Le token OAuth n'est jamais stocké ici : keyring_ref pointe vers le trousseau.
CREATE TABLE IF NOT EXISTS account (
    id           INTEGER PRIMARY KEY,
    platform     TEXT NOT NULL,
    display_name TEXT NOT NULL,
    keyring_ref  TEXT UNIQUE,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_account (
    bot_id     INTEGER NOT NULL REFERENCES bot(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    PRIMARY KEY (bot_id, account_id)
);

-- weekdays : masque de bits, lundi = bit 0. window_start == window_end : toute la journée.
CREATE TABLE IF NOT EXISTS schedule (
    bot_id       INTEGER PRIMARY KEY REFERENCES bot(id) ON DELETE CASCADE,
    every_hours  REAL NOT NULL CHECK (every_hours > 0),
    weekdays     INTEGER NOT NULL DEFAULT 127 CHECK (weekdays BETWEEN 1 AND 127),
    window_start TEXT NOT NULL DEFAULT '00:00',
    window_end   TEXT NOT NULL DEFAULT '00:00',
    timezone     TEXT NOT NULL DEFAULT 'UTC',
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_slot_at REAL
);

CREATE TABLE IF NOT EXISTS clip (
    id          INTEGER PRIMARY KEY,
    bot_id      INTEGER NOT NULL REFERENCES bot(id) ON DELETE CASCADE,
    source_id   INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    start_ms    INTEGER NOT NULL,
    end_ms      INTEGER NOT NULL,
    score       REAL NOT NULL DEFAULT 0,
    title       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    words       TEXT NOT NULL DEFAULT '[]',
    file_path   TEXT,
    status      TEXT NOT NULL DEFAULT 'render'
                CHECK (status IN ('render', 'review', 'rejected', 'scheduled',
                                  'publishing', 'published', 'failed')),
    error       TEXT,
    approved_at REAL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    UNIQUE (source_id, start_ms, end_ms)
);

CREATE TABLE IF NOT EXISTS publish_job (
    id           INTEGER PRIMARY KEY,
    clip_id      INTEGER NOT NULL REFERENCES clip(id) ON DELETE CASCADE,
    account_id   INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    scheduled_at REAL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'queued', 'publishing', 'published', 'failed')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    external_id  TEXT,
    url          TEXT,
    last_error   TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    UNIQUE (clip_id, account_id)
);

-- File de jobs générique (étapes du pipeline et publications).
CREATE TABLE IF NOT EXISTS job (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 5,
    run_after       REAL NOT NULL,
    lease_until     REAL,
    worker          TEXT,
    last_error      TEXT,
    result          TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS job_ready ON job (status, run_after);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Ouvre la base en mode autocommit ; les transactions passent par `tx`."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    init(conn)
    return conn


def init(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transaction en écriture (BEGIN IMMEDIATE). Réentrante : un `tx` imbriqué
    rejoint la transaction en cours."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
