"""Base SQLite : schéma, connexion et transactions.

Toutes les dates sont des timestamps Unix (REAL) pour simplifier les calculs
et permettre une horloge injectable dans les tests.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 8

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


# Migrations appliquées dans l'ordre à une base plus ancienne.
MIGRATIONS: dict[int, str] = {
    2: """
-- Phase 3 : découverte automatique et notation par IA.
ALTER TABLE bot ADD COLUMN ai_settings TEXT NOT NULL DEFAULT '{}';
ALTER TABLE clip ADD COLUMN reason TEXT NOT NULL DEFAULT '';
ALTER TABLE clip ADD COLUMN origin TEXT NOT NULL DEFAULT 'heuristic';
-- Extrait téléchargé (fenêtre) : le rendu part de ce fichier, décalé de media_offset_ms.
ALTER TABLE clip ADD COLUMN media_path TEXT;
ALTER TABLE clip ADD COLUMN media_offset_ms INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS watch (
    id               INTEGER PRIMARY KEY,
    bot_id           INTEGER NOT NULL REFERENCES bot(id) ON DELETE CASCADE,
    platform         TEXT NOT NULL,
    channel          TEXT NOT NULL,
    channel_id       TEXT,
    interval_minutes INTEGER NOT NULL DEFAULT 60 CHECK (interval_minutes >= 5),
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_checked_at  REAL,
    created_at       REAL NOT NULL,
    UNIQUE (bot_id, platform, channel)
);

-- Un contenu n'est découvert qu'une fois par bot (déduplication).
CREATE TABLE IF NOT EXISTS discovered (
    id           INTEGER PRIMARY KEY,
    bot_id       INTEGER NOT NULL REFERENCES bot(id) ON DELETE CASCADE,
    watch_id     INTEGER REFERENCES watch(id) ON DELETE SET NULL,
    source_id    INTEGER REFERENCES source(id) ON DELETE SET NULL,
    platform     TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    channel_id   TEXT,
    stream_id    TEXT,
    url          TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    published_at REAL,
    duration     REAL,
    signal       TEXT,
    status       TEXT NOT NULL DEFAULT 'new'
                 CHECK (status IN ('new', 'presorted', 'scoring', 'deferred', 'done', 'skipped', 'failed')),
    error        TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    UNIQUE (bot_id, platform, external_id)
);

-- Fenêtres retenues par le tri bon marché : seules elles sont téléchargées,
-- transcrites et envoyées à l'IA.
CREATE TABLE IF NOT EXISTS candidate_window (
    id            INTEGER PRIMARY KEY,
    discovered_id INTEGER NOT NULL REFERENCES discovered(id) ON DELETE CASCADE,
    start         REAL NOT NULL,
    end           REAL NOT NULL,
    signal_score  REAL NOT NULL DEFAULT 0,
    media_path    TEXT,
    words         TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'ready', 'scored', 'failed')),
    error         TEXT,
    UNIQUE (discovered_id, start, end)
);

-- Chat enregistré en direct (EventSub). Pas de pseudonymes : horodatage et texte.
CREATE TABLE IF NOT EXISTS chat_message (
    id         INTEGER PRIMARY KEY,
    platform   TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    stream_id  TEXT,
    ts         REAL NOT NULL,
    text       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS chat_by_stream ON chat_message (platform, stream_id, ts);

CREATE TABLE IF NOT EXISTS chat_session (
    platform   TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    stream_id  TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at   REAL,
    PRIMARY KEY (platform, stream_id)
);

CREATE TABLE IF NOT EXISTS ai_usage (
    bot_id        INTEGER NOT NULL REFERENCES bot(id) ON DELETE CASCADE,
    day           TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    calls         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_id, day, provider, model)
);

CREATE TABLE IF NOT EXISTS ai_cache (
    key        TEXT PRIMARY KEY,
    response   TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS alert (
    id         INTEGER PRIMARY KEY,
    bot_id     INTEGER REFERENCES bot(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    day        TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at REAL NOT NULL,
    seen       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (bot_id, kind, day)
);
""",
    3: """
-- Agent identité : propositions de nom, description, visuels et playlists.
CREATE TABLE IF NOT EXISTS identity (
    id            INTEGER PRIMARY KEY,
    bot_id        INTEGER NOT NULL REFERENCES bot(id) ON DELETE CASCADE,
    account_id    INTEGER REFERENCES account(id) ON DELETE SET NULL,
    brief         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'generating'
                  CHECK (status IN ('generating', 'ready', 'applying', 'applied', 'failed')),
    proposal      TEXT,               -- JSON : IdentityProposal
    choice        TEXT NOT NULL DEFAULT '{}',  -- JSON : ce que l'utilisateur garde
    images_status TEXT NOT NULL DEFAULT 'pending'
                  CHECK (images_status IN ('pending', 'generating', 'ready', 'failed')),
    avatar_path   TEXT,
    banner_path   TEXT,
    applied       TEXT NOT NULL DEFAULT '{}',  -- JSON : étapes déjà faites sur YouTube
    error         TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist (
    id          INTEGER PRIMARY KEY,
    bot_id      INTEGER NOT NULL REFERENCES bot(id) ON DELETE CASCADE,
    account_id  INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    keywords    TEXT NOT NULL DEFAULT '[]',  -- JSON ; vide = tous les Shorts du bot
    external_id TEXT,
    created_at  REAL NOT NULL,
    UNIQUE (bot_id, account_id, title)
);

CREATE TABLE IF NOT EXISTS playlist_item (
    playlist_id    INTEGER NOT NULL REFERENCES playlist(id) ON DELETE CASCADE,
    publish_job_id INTEGER NOT NULL REFERENCES publish_job(id) ON DELETE CASCADE,
    added_at       REAL NOT NULL,
    PRIMARY KEY (playlist_id, publish_job_id)
);
""",
    4: """
-- Clips viraux : texte d'accroche incrusté, instant du pic de chat par fenêtre.
ALTER TABLE clip ADD COLUMN hook TEXT NOT NULL DEFAULT '';
ALTER TABLE candidate_window ADD COLUMN peak REAL;
""",
    5: """
-- Éclaireur IA : chaînes trouvées automatiquement à partir d'un thème.
ALTER TABLE watch ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual';  -- manual | ai
ALTER TABLE watch ADD COLUMN reason TEXT NOT NULL DEFAULT '';
ALTER TABLE watch ADD COLUMN score INTEGER;
-- Chaîne proposée par l'IA puis retirée par l'utilisateur : jamais reproposée.
ALTER TABLE watch ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS scout_run (
    id          INTEGER PRIMARY KEY,
    bot_id      INTEGER NOT NULL REFERENCES bot(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
    plan        TEXT,               -- JSON : recherches lancées
    candidates  INTEGER NOT NULL DEFAULT 0,
    added       TEXT NOT NULL DEFAULT '[]',  -- JSON : chaînes ajoutées
    error       TEXT,
    created_at  REAL NOT NULL,
    finished_at REAL
);
""",
    6: """
-- Début du dernier essai de chaque job : durées réelles pour estimer les temps restants.
ALTER TABLE job ADD COLUMN started_at REAL;
""",
    7: """
-- Langue parlée (ISO 639-1, détectée par Whisper) : titres et métadonnées dans cette langue.
ALTER TABLE candidate_window ADD COLUMN language TEXT;
ALTER TABLE clip ADD COLUMN language TEXT;
""",
    8: """
-- Lien public du compte (profil TikTok, Instagram) enregistré à la connexion.
ALTER TABLE account ADD COLUMN url TEXT;
""",
}


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
    if version >= SCHEMA_VERSION:
        return
    if version == 0:
        conn.executescript(SCHEMA)
        version = 1
    for target in range(version + 1, SCHEMA_VERSION + 1):
        conn.executescript(f"BEGIN; {MIGRATIONS[target]} PRAGMA user_version={target}; COMMIT;")


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
