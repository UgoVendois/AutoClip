"""Opérations métier partagées par l'API, la CLI et le dry run."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import DEFAULT_CLIP_SETTINGS, DEFAULT_SETTINGS
from .db import tx
from .jobs.queue import JobQueue
from .scheduler import parse_hhmm, weekday_mask


class ServiceError(ValueError):
    pass


# -- réglages globaux ---------------------------------------------------------

def get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    values = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM setting"):
        values[row["key"]] = json.loads(row["value"])
    return values


def update_settings(conn: sqlite3.Connection, changes: dict[str, Any]) -> dict[str, Any]:
    unknown = set(changes) - set(DEFAULT_SETTINGS)
    if unknown:
        raise ServiceError(f"réglages inconnus : {', '.join(sorted(unknown))}")
    if changes.get("youtube_privacy", "private") not in ("private", "unlisted", "public"):
        raise ServiceError("youtube_privacy : private, unlisted ou public")
    with tx(conn):
        for key, value in changes.items():
            conn.execute(
                "INSERT INTO setting (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
    return get_settings(conn)


# -- bots -----------------------------------------------------------------------

def bot_settings(row: sqlite3.Row) -> dict[str, Any]:
    return {**DEFAULT_CLIP_SETTINGS, **json.loads(row["clip_settings"])}


def create_bot(conn: sqlite3.Connection, name: str, *, dry_run: bool | None = None,
               clip_settings: dict[str, Any] | None = None) -> int:
    if dry_run is None:
        dry_run = bool(get_settings(conn)["default_dry_run"])
    _check_clip_settings(clip_settings or {})
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO bot (name, clip_settings, dry_run, created_at) VALUES (?, ?, ?, ?)",
            (name.strip() or "Bot", json.dumps(clip_settings or {}), int(dry_run), time.time()),
        )
    return cur.lastrowid


def update_bot(conn: sqlite3.Connection, bot_id: int, *, name: str | None = None,
               dry_run: bool | None = None, clip_settings: dict[str, Any] | None = None) -> None:
    bot = get_bot(conn, bot_id)
    with tx(conn):
        if name is not None:
            conn.execute("UPDATE bot SET name = ? WHERE id = ?", (name.strip(), bot_id))
        if dry_run is not None:
            conn.execute("UPDATE bot SET dry_run = ? WHERE id = ?", (int(dry_run), bot_id))
        if clip_settings is not None:
            _check_clip_settings(clip_settings)
            merged = {**json.loads(bot["clip_settings"]), **clip_settings}
            conn.execute("UPDATE bot SET clip_settings = ? WHERE id = ?", (json.dumps(merged), bot_id))


def delete_bot(conn: sqlite3.Connection, bot_id: int) -> None:
    get_bot(conn, bot_id)
    with tx(conn):
        conn.execute("DELETE FROM bot WHERE id = ?", (bot_id,))


def get_bot(conn: sqlite3.Connection, bot_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM bot WHERE id = ?", (bot_id,)).fetchone()
    if row is None:
        raise ServiceError(f"bot {bot_id} introuvable")
    return row


def _check_clip_settings(values: dict[str, Any]) -> None:
    unknown = set(values) - set(DEFAULT_CLIP_SETTINGS)
    if unknown:
        raise ServiceError(f"paramètres de clipping inconnus : {', '.join(sorted(unknown))}")
    merged = {**DEFAULT_CLIP_SETTINGS, **values}
    if not 5 <= merged["min_seconds"] <= merged["max_seconds"] <= 180:
        raise ServiceError("il faut 5 <= min_seconds <= max_seconds <= 180")


# -- sources --------------------------------------------------------------------

def add_source(conn: sqlite3.Connection, queue: JobQueue, bot_id: int, uri: str) -> int:
    get_bot(conn, bot_id)
    uri = uri.strip()
    if uri.startswith(("http://", "https://")):
        kind = "url"
    else:
        path = Path(uri).expanduser().resolve()
        if not path.is_file():
            raise ServiceError(f"fichier introuvable : {path}")
        kind, uri = "file", str(path)
    with tx(conn):
        conn.execute(
            "INSERT INTO source (bot_id, kind, uri, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (bot_id, uri) DO NOTHING",
            (bot_id, kind, uri, time.time()),
        )
        source_id = conn.execute("SELECT id FROM source WHERE bot_id = ? AND uri = ?",
                                 (bot_id, uri)).fetchone()["id"]
        queue.enqueue("ingest", {"source_id": source_id}, key=f"ingest:{source_id}")
    return source_id


# -- comptes ----------------------------------------------------------------------

def create_dry_account(conn: sqlite3.Connection, name: str = "dry-run") -> int:
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO account (platform, display_name, created_at) VALUES ('youtube', ?, ?)",
            (name, time.time()),
        )
    return cur.lastrowid


def link_account(conn: sqlite3.Connection, bot_id: int, account_id: int) -> None:
    get_bot(conn, bot_id)
    if conn.execute("SELECT 1 FROM account WHERE id = ?", (account_id,)).fetchone() is None:
        raise ServiceError(f"compte {account_id} introuvable")
    with tx(conn):
        conn.execute("INSERT OR IGNORE INTO bot_account (bot_id, account_id) VALUES (?, ?)",
                     (bot_id, account_id))


def unlink_account(conn: sqlite3.Connection, bot_id: int, account_id: int) -> None:
    with tx(conn):
        conn.execute("DELETE FROM bot_account WHERE bot_id = ? AND account_id = ?", (bot_id, account_id))


# -- validation des clips -------------------------------------------------------

def approve_clip(conn: sqlite3.Connection, clip_id: int, *, now: float | None = None,
                 title: str | None = None, description: str | None = None) -> int:
    """Valide un clip : crée une publication en attente par compte du bot.
    Renvoie le nombre de publications créées."""
    now = time.time() if now is None else now
    with tx(conn):
        clip = conn.execute("SELECT * FROM clip WHERE id = ?", (clip_id,)).fetchone()
        if clip is None:
            raise ServiceError(f"clip {clip_id} introuvable")
        if clip["status"] != "review":
            raise ServiceError(f"clip {clip_id} : statut {clip['status']!r}, attendu 'review'")
        accounts = [r["account_id"] for r in conn.execute(
            "SELECT account_id FROM bot_account WHERE bot_id = ?", (clip["bot_id"],))]
        if not accounts:
            raise ServiceError("ce bot n'a aucun compte cible : `autoclip account link`")
        conn.execute(
            """UPDATE clip SET status = 'scheduled', approved_at = ?, updated_at = ?,
                               title = COALESCE(?, title), description = COALESCE(?, description)
               WHERE id = ?""",
            (now, now, title, description, clip_id),
        )
        for account_id in accounts:
            conn.execute(
                """INSERT OR IGNORE INTO publish_job (clip_id, account_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (clip_id, account_id, now, now),
            )
    return len(accounts)


def reject_clip(conn: sqlite3.Connection, clip_id: int) -> None:
    with tx(conn):
        cur = conn.execute(
            "UPDATE clip SET status = 'rejected', updated_at = ? WHERE id = ? AND status = 'review'",
            (time.time(), clip_id),
        )
    if cur.rowcount != 1:
        raise ServiceError(f"clip {clip_id} introuvable ou pas en attente de validation")


def list_clips(conn: sqlite3.Connection, *, bot_id: int | None = None,
               status: str | None = None) -> list[sqlite3.Row]:
    query, params = "SELECT * FROM clip WHERE 1 = 1", []
    if bot_id is not None:
        query += " AND bot_id = ?"
        params.append(bot_id)
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    return conn.execute(query + " ORDER BY id", params).fetchall()


# -- planning -------------------------------------------------------------------

def set_schedule(conn: sqlite3.Connection, bot_id: int, *, every_hours: float,
                 days: str | list[str] = "all", window: str = "00:00-00:00",
                 timezone: str = "UTC", enabled: bool = True) -> None:
    get_bot(conn, bot_id)
    if every_hours <= 0:
        raise ServiceError("every_hours doit être positif")
    try:
        start, end = (part.strip() for part in window.split("-"))
        parse_hhmm(start), parse_hhmm(end)
        ZoneInfo(timezone)
        mask = weekday_mask(days)
    except (ValueError, KeyError) as exc:
        raise ServiceError(f"planning invalide : {exc}") from exc
    except Exception as exc:  # ZoneInfoNotFoundError
        raise ServiceError(f"fuseau horaire inconnu : {timezone}") from exc
    with tx(conn):
        conn.execute(
            """INSERT INTO schedule (bot_id, every_hours, weekdays, window_start, window_end,
                                     timezone, enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (bot_id) DO UPDATE SET
                   every_hours = excluded.every_hours, weekdays = excluded.weekdays,
                   window_start = excluded.window_start, window_end = excluded.window_end,
                   timezone = excluded.timezone, enabled = excluded.enabled""",
            (bot_id, every_hours, mask, start, end, timezone, int(enabled)),
        )


def bot_stats(conn: sqlite3.Connection, bot_id: int) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM clip WHERE bot_id = ? GROUP BY status",
                        (bot_id,))
    return {r["status"]: r["n"] for r in rows}
