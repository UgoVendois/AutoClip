"""Opérations métier partagées par l'API, la CLI et le dry run."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import DEFAULT_AI_SETTINGS, DEFAULT_CLIP_SETTINGS, DEFAULT_SETTINGS
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
    if changes.get("ui_theme", "system") not in ("system", "light", "dark"):
        raise ServiceError("ui_theme : system, light ou dark")
    if changes.get("identity_provider", "anthropic") not in AI_PROVIDERS[1:]:
        raise ServiceError(f"identity_provider : {', '.join(AI_PROVIDERS[1:])}")
    if changes.get("image_provider", "svg") not in ("svg", "gemini", "openai", "fake"):
        raise ServiceError("image_provider : svg, gemini, openai ou fake")
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


def ai_settings(row: sqlite3.Row) -> dict[str, Any]:
    return {**DEFAULT_AI_SETTINGS, **json.loads(row["ai_settings"])}


AI_PROVIDERS = ("none", "anthropic", "gemini", "openai", "mistral", "ollama", "fake")


def _check_ai_settings(values: dict[str, Any]) -> None:
    unknown = set(values) - set(DEFAULT_AI_SETTINGS)
    if unknown:
        raise ServiceError(f"réglages IA inconnus : {', '.join(sorted(unknown))}")
    merged = {**DEFAULT_AI_SETTINGS, **values}
    if merged["provider"] not in AI_PROVIDERS:
        raise ServiceError(f"fournisseur d'IA : {', '.join(AI_PROVIDERS)}")
    if merged["effort"] not in ("low", "medium", "high"):
        raise ServiceError("effort : low, medium ou high")
    if not isinstance(merged["daily_token_budget"], int) or merged["daily_token_budget"] < 0:
        raise ServiceError("daily_token_budget : entier positif (tokens par jour)")
    if not 1 <= merged["max_windows"] <= 30:
        raise ServiceError("max_windows : entre 1 et 30")
    if not 0 <= merged["min_score"] <= 100:
        raise ServiceError("min_score : entre 0 et 100")
    if not set(merged["scout_platforms"]) <= {"twitch", "youtube"}:
        raise ServiceError("scout_platforms : twitch et/ou youtube")
    if not 1 <= merged["scout_max_channels"] <= 30:
        raise ServiceError("scout_max_channels : entre 1 et 30")
    if not 1 <= merged["scout_every_hours"] <= 24 * 7:
        raise ServiceError("scout_every_hours : entre 1 et 168")
    if merged["scout_enabled"] and not str(merged["scout_theme"]).strip():
        raise ServiceError("décris un thème pour que l'IA cherche des chaînes")


def update_bot(conn: sqlite3.Connection, bot_id: int, *, name: str | None = None,
               dry_run: bool | None = None, clip_settings: dict[str, Any] | None = None,
               ai_settings: dict[str, Any] | None = None) -> None:
    bot = get_bot(conn, bot_id)
    if ai_settings is not None:
        _check_ai_settings({**json.loads(bot["ai_settings"]), **ai_settings})
    with tx(conn):
        if ai_settings is not None:
            merged_ai = {**json.loads(bot["ai_settings"]), **ai_settings}
            conn.execute("UPDATE bot SET ai_settings = ? WHERE id = ?", (json.dumps(merged_ai), bot_id))
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


def account_url(platform: str, keyring_ref: str | None) -> str | None:
    """Page publique de la chaîne (l'identifiant vient de la référence du jeton, pas du secret)."""
    if platform == "youtube" and keyring_ref and keyring_ref.startswith("token:youtube:"):
        return f"https://www.youtube.com/channel/{keyring_ref.removeprefix('token:youtube:')}"
    return None


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
    if merged["layout"] not in ("auto", "facecam", "fit", "crop"):
        raise ServiceError("layout : auto, facecam, fit ou crop")
    if not 0 <= merged["hook_seconds"] <= 10:
        raise ServiceError("hook_seconds : entre 0 et 10")


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


def approve_all(conn: sqlite3.Connection, bot_id: int, *, now: float | None = None,
                edits: dict[int, dict[str, str]] | None = None) -> int:
    """Valide tous les clips en attente, les meilleurs scores d'abord (publiés en premier).
    `edits` : titres et descriptions modifiés à la main, par clip. Renvoie le nombre validé."""
    edits = edits or {}
    get_bot(conn, bot_id)
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM clip WHERE bot_id = ? AND status = 'review' "
        "ORDER BY score DESC, id", (bot_id,))]
    now = time.time() if now is None else now
    for i, clip_id in enumerate(ids):
        e = edits.get(clip_id, {})
        approve_clip(conn, clip_id, now=now + i * 1e-3,  # garde l'ordre d'approbation
                     title=e.get("title"), description=e.get("description"))
    return len(ids)


def publish_now(conn: sqlite3.Connection, queue: JobQueue, publish_job_id: int, *,
                now: float | None = None) -> None:
    """Envoie tout de suite une publication programmée. Les créneaux suivants
    repartent de maintenant, avec l'espacement du planning."""
    now = time.time() if now is None else now
    row = conn.execute("SELECT pj.*, c.bot_id FROM publish_job pj JOIN clip c ON c.id = pj.clip_id "
                       "WHERE pj.id = ?", (publish_job_id,)).fetchone()
    if row is None:
        raise ServiceError("publication introuvable")
    if row["status"] != "pending":
        raise ServiceError("cette publication est déjà partie")
    with tx(conn):
        conn.execute("UPDATE publish_job SET status = 'queued', scheduled_at = ?, updated_at = ? WHERE id = ?",
                     (now, now, publish_job_id))
        queue.enqueue("publish", {"publish_job_id": publish_job_id}, key=f"publish:{publish_job_id}")
        # Les autres réservations du bot sont recalculées à partir de cet envoi.
        conn.execute("UPDATE publish_job SET scheduled_at = NULL, updated_at = ? WHERE status = 'pending' "
                     "AND clip_id IN (SELECT id FROM clip WHERE bot_id = ?) AND clip_id != ?",
                     (now, row["bot_id"], row["clip_id"]))
        conn.execute("UPDATE schedule SET last_slot_at = ? WHERE bot_id = ?", (now, row["bot_id"]))


def release_slots(conn: sqlite3.Connection, bot_id: int) -> None:
    """Libère le créneau réservé (calculé avec l'ancien planning) : le planificateur
    en recalcule un, espacé depuis la dernière vraie publication."""
    with tx(conn):
        conn.execute("UPDATE publish_job SET scheduled_at = NULL WHERE status = 'pending' "
                     "AND clip_id IN (SELECT id FROM clip WHERE bot_id = ?)", (bot_id,))
        last = conn.execute("SELECT MAX(pj.scheduled_at) FROM publish_job pj JOIN clip c ON c.id = pj.clip_id "
                            "WHERE c.bot_id = ? AND pj.status != 'pending'", (bot_id,)).fetchone()[0]
        conn.execute("UPDATE schedule SET last_slot_at = ? WHERE bot_id = ?", (last, bot_id))


def set_hook(conn: sqlite3.Connection, queue: JobQueue, clip_id: int, hook: str) -> None:
    """Change le texte d'accroche d'un clip en attente de validation : il est
    re-rendu (l'accroche est incrustée dans la vidéo)."""
    hook = hook.strip()[:60]
    with tx(conn):
        clip = conn.execute("SELECT status, hook FROM clip WHERE id = ?", (clip_id,)).fetchone()
        if clip is None:
            raise ServiceError(f"clip {clip_id} introuvable")
        if clip["status"] not in ("review", "failed"):
            raise ServiceError("l'accroche se modifie seulement avant validation")
        if clip["hook"] == hook:
            return
        conn.execute("UPDATE clip SET hook = ?, status = 'render', updated_at = ? WHERE id = ?",
                     (hook, time.time(), clip_id))
        queue.enqueue("render", {"clip_id": clip_id}, key=f"render:{clip_id}:{short_key(hook)}")


def short_key(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:10]


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
    release_slots(conn, bot_id)


def bot_stats(conn: sqlite3.Connection, bot_id: int) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM clip WHERE bot_id = ? GROUP BY status",
                        (bot_id,))
    return {r["status"]: r["n"] for r in rows}


# -- découverte automatique ---------------------------------------------------------

def add_watch(conn: sqlite3.Connection, bot_id: int, platform: str, channel: str,
              interval_minutes: int = 60, *, now: float | None = None) -> int:
    from .discovery import REGISTRY

    get_bot(conn, bot_id)
    if platform not in REGISTRY:
        raise ServiceError(f"plateforme : {', '.join(sorted(REGISTRY))}")
    if interval_minutes < 5:
        raise ServiceError("vérification au plus toutes les 5 minutes")
    channel = channel.strip()
    if not channel:
        raise ServiceError("chaîne vide")
    with tx(conn):
        conn.execute(
            """INSERT INTO watch (bot_id, platform, channel, interval_minutes, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (bot_id, platform, channel) DO UPDATE SET
                   interval_minutes = excluded.interval_minutes, enabled = 1, dismissed = 0,
                   origin = 'manual'""",
            (bot_id, platform, channel, interval_minutes, time.time() if now is None else now))
        return conn.execute("SELECT id FROM watch WHERE bot_id = ? AND platform = ? AND channel = ?",
                            (bot_id, platform, channel)).fetchone()["id"]


# -- annulation d'une analyse ---------------------------------------------------------

CANCELLED = "annulé"  # erreur posée sur un contenu ou une source annulés par l'utilisateur
ACTIVE_DISCOVERED = ("new", "presorted", "scoring", "deferred")
ACTIVE_SOURCE = ("ingest", "transcribe", "analyze")


def is_cancelled(row: sqlite3.Row | None) -> bool:
    return row is not None and row["error"] == CANCELLED


def cancel_discovered(conn: sqlite3.Connection, discovered_id: int) -> None:
    """Arrête l'analyse d'une VOD. Le travail en cours s'interrompt (téléchargement,
    mesure du son) ; les étapes suivantes ne démarrent pas. Les clips déjà faits restent."""
    row = conn.execute("SELECT status, source_id FROM discovered WHERE id = ?", (discovered_id,)).fetchone()
    if row is None:
        raise ServiceError("contenu introuvable")
    if row["status"] not in ACTIVE_DISCOVERED:
        raise ServiceError("cette analyse est déjà terminée")
    with tx(conn):
        conn.execute("UPDATE discovered SET status = 'skipped', error = ?, updated_at = ? WHERE id = ?",
                     (CANCELLED, time.time(), discovered_id))
        conn.execute("UPDATE candidate_window SET status = 'failed', error = ? "
                     "WHERE discovered_id = ? AND status IN ('pending', 'ready')", (CANCELLED, discovered_id))
        if row["source_id"]:
            conn.execute("UPDATE source SET status = 'failed', error = ? WHERE id = ? AND status IN (?, ?, ?)",
                         (CANCELLED, row["source_id"], *ACTIVE_SOURCE))


def cancel_source(conn: sqlite3.Connection, source_id: int) -> None:
    row = conn.execute("SELECT status FROM source WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise ServiceError("source introuvable")
    linked = conn.execute("SELECT id FROM discovered WHERE source_id = ? AND status IN (?, ?, ?, ?)",
                          (source_id, *ACTIVE_DISCOVERED)).fetchone()
    if linked is not None:  # VOD trouvée par la découverte : on arrête toute son analyse
        cancel_discovered(conn, linked["id"])
        return
    if row["status"] not in ACTIVE_SOURCE:
        raise ServiceError("cette analyse est déjà terminée")
    with tx(conn):
        conn.execute("UPDATE source SET status = 'failed', error = ? WHERE id = ?", (CANCELLED, source_id))


def remove_watch(conn: sqlite3.Connection, watch_id: int) -> None:
    """Supprime une chaîne surveillée. Une chaîne trouvée par l'IA est seulement
    écartée, pour que l'éclaireur ne la repropose pas."""
    with tx(conn):
        row = conn.execute("SELECT origin FROM watch WHERE id = ?", (watch_id,)).fetchone()
        if row is None:
            raise ServiceError(f"surveillance {watch_id} introuvable")
        if row["origin"] == "ai":
            conn.execute("UPDATE watch SET dismissed = 1, enabled = 0 WHERE id = ?", (watch_id,))
        else:
            conn.execute("DELETE FROM watch WHERE id = ?", (watch_id,))


def update_watch(conn: sqlite3.Connection, watch_id: int, *, enabled: bool | None = None,
                 interval_minutes: int | None = None) -> None:
    with tx(conn):
        if conn.execute("SELECT 1 FROM watch WHERE id = ? AND dismissed = 0", (watch_id,)).fetchone() is None:
            raise ServiceError(f"surveillance {watch_id} introuvable")
        if interval_minutes is not None:
            if interval_minutes < 5:
                raise ServiceError("vérification au plus toutes les 5 minutes")
            conn.execute("UPDATE watch SET interval_minutes = ? WHERE id = ?", (interval_minutes, watch_id))
        if enabled is not None:
            conn.execute("UPDATE watch SET enabled = ? WHERE id = ?", (int(enabled), watch_id))


def list_alerts(conn: sqlite3.Connection, *, unseen_only: bool = False) -> list[sqlite3.Row]:
    query = "SELECT * FROM alert" + (" WHERE seen = 0" if unseen_only else "") + " ORDER BY created_at DESC LIMIT 100"
    return conn.execute(query).fetchall()
