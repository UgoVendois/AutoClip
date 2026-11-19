"""Temps restant estimé avant que les clips d'une VOD ou d'une source soient prêts.

Le worker traite un job à la fois, dans l'ordre (`run_after`, id). On rejoue donc la
file : le job en cours, ceux en attente, et les étapes qu'ils vont créer (une VOD
analysée crée ses fenêtres, la dernière fenêtre lance la notation, la notation crée
les rendus...). Chaque étape dure ce que les dernières du même type ont réellement
duré sur cette machine (`job.started_at`), sinon une valeur par défaut mesurée.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass, field

from . import services
from .config import DEFAULT_AI_SETTINGS, DEFAULT_CLIP_SETTINGS

# Valeurs mesurées sur un portable à 12 cœurs (Whisper medium, CPU), en secondes.
DEFAULT_SECONDS = {
    "prepare_window": 40.0,  # téléchargement d'un passage + transcription
    "ai_score": 45.0,
    "render": 25.0,
    "ingest": 90.0,
    "analyze": 5.0,
    "identity_generate": 30.0,
    "identity_images": 40.0,  # photo de profil + bannière
}
PRESORT_PER_SECOND = 0.03  # téléchargement de l'audio + mesure du volume, par seconde de VOD
TRANSCRIBE_PER_SECOND = 0.35  # Whisper medium, par seconde de vidéo
DEFAULT_MEDIA_SECONDS = 1800.0  # durée supposée d'une source pas encore téléchargée
OTHER_SECONDS = 5.0  # découverte, publication, etc.
SAMPLE = 20  # derniers jobs terminés pris en compte par type
MAX_STEPS = 20000


@dataclass
class _Sim:
    conn: sqlite3.Connection
    now: float
    durations: dict[str, float] = field(default_factory=dict)
    rates: dict[str, float] = field(default_factory=dict)
    finish: dict[tuple[str, int], float] = field(default_factory=dict)  # ("d"|"s", id) -> fin
    pending_windows: dict[int, int] = field(default_factory=dict)
    settings_cache: dict[int, tuple[dict, dict]] = field(default_factory=dict)


def _measured(conn: sqlite3.Connection) -> tuple[dict[str, float], dict[str, float]]:
    """Durée médiane par type de job, et vitesse (s de calcul par s de média)
    pour les étapes qui dépendent de la longueur de la vidéo."""
    durations: dict[str, float] = {}
    for kind in DEFAULT_SECONDS:
        rows = conn.execute(
            "SELECT updated_at - started_at AS d FROM job WHERE kind = ? AND status = 'done' "
            "AND started_at IS NOT NULL AND COALESCE(result, '') NOT LIKE '%skipped%' "
            "AND COALESCE(result, '') NOT LIKE '%cancelled%' "
            "ORDER BY updated_at DESC LIMIT ?", (kind, SAMPLE)).fetchall()
        values = [r["d"] for r in rows if r["d"] is not None and r["d"] > 0]
        if values:
            durations[kind] = statistics.median(values)
    rates: dict[str, float] = {}
    rows = conn.execute(
        """SELECT (j.updated_at - j.started_at) / d.duration AS r FROM job j
           JOIN discovered d ON d.id = CAST(json_extract(j.payload, '$.discovered_id') AS INTEGER)
           WHERE j.kind = 'presort' AND j.status = 'done' AND j.started_at IS NOT NULL
             AND d.duration > 0 AND j.result LIKE '%windows%'
           ORDER BY j.updated_at DESC LIMIT ?""", (SAMPLE,)).fetchall()
    values = [r["r"] for r in rows if r["r"] and r["r"] > 0]
    if values:
        rates["presort"] = statistics.median(values)
    return durations, rates


def _settings(sim: _Sim, bot_id: int) -> tuple[dict, dict]:
    if bot_id not in sim.settings_cache:
        row = sim.conn.execute("SELECT * FROM bot WHERE id = ?", (bot_id,)).fetchone()
        sim.settings_cache[bot_id] = ((services.ai_settings(row), services.bot_settings(row)) if row
                                      else (dict(DEFAULT_AI_SETTINGS), dict(DEFAULT_CLIP_SETTINGS)))
    return sim.settings_cache[bot_id]


def _discovered(sim: _Sim, discovered_id: int) -> sqlite3.Row | None:
    return sim.conn.execute("SELECT * FROM discovered WHERE id = ?", (discovered_id,)).fetchone()


def _cost(sim: _Sim, kind: str, payload: dict) -> float:
    if kind == "presort":
        d = _discovered(sim, payload.get("discovered_id", 0))
        length = (d["duration"] if d and d["duration"] else 4 * 3600)
        return length * sim.rates.get("presort", PRESORT_PER_SECOND)
    if kind == "transcribe":
        return DEFAULT_MEDIA_SECONDS * TRANSCRIBE_PER_SECOND
    return sim.durations.get(kind, DEFAULT_SECONDS.get(kind, OTHER_SECONDS))


def _owner(sim: _Sim, kind: str, payload: dict) -> tuple[str, int] | None:
    """À quelle VOD (« d »), source manuelle (« s ») ou génération de visuels (« i »)
    ce job appartient."""
    if kind in ("identity_generate", "identity_images"):
        return ("i", payload["identity_id"]) if "identity_id" in payload else None
    if "discovered_id" in payload:
        return ("d", payload["discovered_id"])
    if "window_id" in payload:
        w = sim.conn.execute("SELECT discovered_id FROM candidate_window WHERE id = ?",
                             (payload["window_id"],)).fetchone()
        return ("d", w["discovered_id"]) if w else None
    if "clip_id" in payload:
        c = sim.conn.execute("SELECT source_id FROM clip WHERE id = ?", (payload["clip_id"],)).fetchone()
        if c is None:
            return None
        d = sim.conn.execute("SELECT id FROM discovered WHERE source_id = ?", (c["source_id"],)).fetchone()
        return ("d", d["id"]) if d else ("s", c["source_id"])
    if "source_id" in payload:
        return ("s", payload["source_id"])
    return None


def _follow_ups(sim: _Sim, kind: str, payload: dict, owner: tuple[str, int] | None) -> list[tuple[str, dict]]:
    """Jobs que ce job va créer en se terminant (au mieux : nombre maximal)."""
    if owner is None:
        return []
    if owner[0] == "i":
        return [("identity_images", {"identity_id": owner[1]})] if kind == "identity_generate" else []
    if owner[0] == "d":
        d = _discovered(sim, owner[1])
        if d is None or services.is_cancelled(d):
            return []
        ai, _ = _settings(sim, d["bot_id"])
        if kind == "presort":
            n = ai["max_windows"]
            sim.pending_windows[owner[1]] = n
            return [("prepare_window", {"discovered_id": owner[1]})] * n
        if kind == "prepare_window":
            left = sim.pending_windows.get(owner[1], 1) - 1
            sim.pending_windows[owner[1]] = left
            return [("ai_score", {"discovered_id": owner[1]})] if left == 0 else []
        if kind == "ai_score":
            return [("render", {"discovered_id": owner[1]})] * ai["clips_per_video"]
        return []
    src = sim.conn.execute("SELECT bot_id FROM source WHERE id = ?", (owner[1],)).fetchone()
    if src is None:
        return []
    _, clip_cfg = _settings(sim, src["bot_id"])
    nxt = {"ingest": "transcribe", "transcribe": "analyze"}.get(kind)
    if nxt:
        return [(nxt, {"source_id": owner[1]})]
    if kind == "analyze":
        return [("render", {"source_id": owner[1]})] * clip_cfg["max_clips"]
    return []


def estimate(conn: sqlite3.Connection, now: float) -> dict[tuple[str, int], float]:
    """Secondes restantes par contenu : {("d", discovered_id) | ("s", source_id): secondes}."""
    durations, rates = _measured(conn)
    sim = _Sim(conn=conn, now=now, durations=durations, rates=rates)
    for row in conn.execute("SELECT discovered_id, COUNT(*) n FROM candidate_window "
                            "WHERE status = 'pending' GROUP BY discovered_id"):
        sim.pending_windows[row["discovered_id"]] = row["n"]

    t = now
    running = conn.execute("SELECT * FROM job WHERE status = 'running' AND lease_until >= ? "
                           "ORDER BY started_at", (now,)).fetchall()
    queue: list[tuple[float, int, str, dict, tuple[str, int] | None]] = []
    seq = 0
    for job in running:  # un seul en pratique ; l'avance déjà faite est déduite
        payload = json.loads(job["payload"])
        owner = _owner(sim, job["kind"], payload)
        elapsed = now - (job["started_at"] or now)
        t += max(_cost(sim, job["kind"], payload) - elapsed, 0.1 * _cost(sim, job["kind"], payload))
        if owner:
            sim.finish[owner] = t
        for kind, p in _follow_ups(sim, job["kind"], payload, owner):
            seq += 1
            queue.append((t, seq, kind, p, owner))
    for job in conn.execute("SELECT * FROM job WHERE status = 'queued' ORDER BY run_after, id"):
        payload = json.loads(job["payload"])
        seq += 1
        queue.append((job["run_after"], -1_000_000 + seq, job["kind"], payload,
                      _owner(sim, job["kind"], payload)))

    for _ in range(MAX_STEPS):
        if not queue:
            break
        ready = [q for q in queue if q[0] <= t]
        item = min(ready or queue, key=lambda q: (q[0], q[1]))
        queue.remove(item)
        run_after, _, kind, payload, owner = item
        t = max(t, run_after) + _cost(sim, kind, payload)
        if owner:
            sim.finish[owner] = t
        for nxt, p in _follow_ups(sim, kind, payload, owner):
            seq += 1
            queue.append((t, seq, nxt, p, owner))
    return {key: max(end - now, 0.0) for key, end in sim.finish.items()}
