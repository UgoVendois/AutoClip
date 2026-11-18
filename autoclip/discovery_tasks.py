"""Jobs de la découverte automatique.

discover -> presort -> prepare_window (une par fenêtre) -> ai_score -> render (existant)

Le principe d'économie : rien n'est envoyé à l'IA avant le tri bon marché.
Seules les fenêtres autour des pics (chat, commentaires ou énergie audio) sont
téléchargées, transcrites, puis notées en un seul appel par vidéo.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sqlite3
from pathlib import Path

from . import services
from .ai.base import Segment, SegmentScore
from .ai.budget import score_with_budget, today
from .ai.providers import make_provider
from .db import tx
from .discovery import DiscoveredItem, get_discoverer
from .jobs.queue import PermanentError
from .jobs.worker import Task
from .pipeline import analyze
from .pipeline.cache import Cache
from .signals.audio import energy_windows, loudness_series
from .signals.peaks import peak_windows

log = logging.getLogger(__name__)


def _discovered(ctx, discovered_id: int) -> sqlite3.Row:
    row = ctx.conn.execute("SELECT * FROM discovered WHERE id = ?", (discovered_id,)).fetchone()
    if row is None:
        raise PermanentError(f"contenu découvert {discovered_id} supprimé")
    return row


def _item(row: sqlite3.Row) -> DiscoveredItem:
    return DiscoveredItem(external_id=row["external_id"], url=row["url"], title=row["title"],
                          published_at=row["published_at"] or 0, duration=row["duration"],
                          channel_id=row["channel_id"], stream_id=row["stream_id"])


def _set(ctx, table: str, row_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    ctx.conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*fields.values(), row_id))


def _work_dir(ctx, row: sqlite3.Row) -> Path:
    path = ctx.cache.root / "discovery" / f"{row['platform']}-{row['external_id']}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    return float(out.stdout.strip() or 0)


# -- discover -----------------------------------------------------------------------

def run_discover(ctx, payload: dict) -> dict:
    watch = ctx.conn.execute("SELECT * FROM watch WHERE id = ?", (payload["watch_id"],)).fetchone()
    if watch is None or not watch["enabled"]:
        return {"skipped": True}
    bot = services.get_bot(ctx.conn, watch["bot_id"])
    ai = services.ai_settings(bot)
    disc = get_discoverer(watch["platform"])
    channel_id = watch["channel_id"]
    if not channel_id:
        channel_id = disc.resolve_channel(watch["channel"])
        with tx(ctx.conn):
            _set(ctx, "watch", watch["id"], channel_id=channel_id)
    # Fenêtre fixe depuis l'ajout de la chaîne (moins la reprise) : une VOD Twitch
    # porte la date du début du live, elle peut apparaître après un passage.
    since = watch["created_at"] - ai["backfill_hours"] * 3600
    items = disc.list_recent(channel_id, since)
    now = ctx.clock()
    new = 0
    with tx(ctx.conn):
        for item in items:
            if item.live:
                continue  # live en cours : la VOD sera reprise une fois terminée
            cur = ctx.conn.execute(
                """INSERT OR IGNORE INTO discovered (bot_id, watch_id, platform, external_id, channel_id,
                       stream_id, url, title, published_at, duration, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (watch["bot_id"], watch["id"], watch["platform"], item.external_id, channel_id,
                 item.stream_id, item.url, item.title, item.published_at, item.duration, now, now))
            if cur.rowcount:
                new += 1
                ctx.queue.enqueue("presort", {"discovered_id": cur.lastrowid}, key=f"presort:{cur.lastrowid}")
        _set(ctx, "watch", watch["id"], last_checked_at=now)
    return {"found": len(items), "new": new}


# -- presort : tri bon marché, aucun appel à l'IA -------------------------------------

def run_presort(ctx, payload: dict) -> dict:
    row = _discovered(ctx, payload["discovered_id"])
    if row["status"] != "new":
        return {"skipped": row["status"]}
    bot = services.get_bot(ctx.conn, row["bot_id"])
    ai = services.ai_settings(bot)
    disc = get_discoverer(row["platform"])
    item = _item(row)
    signal = disc.signal(ctx.conn, item)

    if signal is not None:
        duration = row["duration"] or (max(signal.events, default=0) + ai["density_seconds"])
        windows = peak_windows(signal.events, duration, window=ai["density_seconds"],
                               top_n=ai["max_windows"], margin=ai["margin_seconds"])
        kind = signal.kind
    else:
        audio = disc.download_audio(item, _work_dir(ctx, row) / "audio")
        duration = row["duration"] or media_duration(audio)
        windows = energy_windows(loudness_series(audio), duration, window=ai["density_seconds"],
                                 top_n=ai["max_windows"], margin=ai["margin_seconds"])
        kind = "audio"

    with tx(ctx.conn):
        if not windows:
            _set(ctx, "discovered", row["id"], status="skipped", signal=kind,
                 error="aucun pic détecté", updated_at=ctx.clock())
            return {"windows": 0}
        ctx.conn.execute(
            "INSERT OR IGNORE INTO source (bot_id, kind, uri, status, created_at) VALUES (?, 'url', ?, 'analyze', ?)",
            (row["bot_id"], row["url"], ctx.clock()))
        source_id = ctx.conn.execute("SELECT id FROM source WHERE bot_id = ? AND uri = ?",
                                     (row["bot_id"], row["url"])).fetchone()["id"]
        for w in windows:
            ctx.conn.execute(
                "INSERT OR IGNORE INTO candidate_window (discovered_id, start, end, signal_score, peak) "
                "VALUES (?, ?, ?, ?, ?)",
                (row["id"], round(w.start, 2), round(w.end, 2), w.score, round(w.peak, 2)))
        _set(ctx, "discovered", row["id"], status="presorted", signal=kind, source_id=source_id,
             duration=duration, updated_at=ctx.clock())
        for w in ctx.conn.execute("SELECT id FROM candidate_window WHERE discovered_id = ?", (row["id"],)):
            ctx.queue.enqueue("prepare_window", {"window_id": w["id"]}, key=f"prepare_window:{w['id']}")
    return {"windows": len(windows), "signal": kind}


# -- prepare_window : téléchargement et transcription d'une fenêtre ---------------------

def _maybe_score(ctx, discovered_id: int) -> None:
    pending = ctx.conn.execute(
        "SELECT COUNT(*) FROM candidate_window WHERE discovered_id = ? AND status = 'pending'",
        (discovered_id,)).fetchone()[0]
    if pending == 0:
        with tx(ctx.conn):
            ctx.conn.execute("UPDATE discovered SET status = 'scoring' WHERE id = ? AND status = 'presorted'",
                             (discovered_id,))
            ctx.queue.enqueue("ai_score", {"discovered_id": discovered_id}, key=f"ai_score:{discovered_id}")


def run_prepare_window(ctx, payload: dict) -> dict:
    w = ctx.conn.execute("SELECT * FROM candidate_window WHERE id = ?", (payload["window_id"],)).fetchone()
    if w is None:
        raise PermanentError(f"fenêtre {payload['window_id']} supprimée")
    if w["status"] != "pending":
        return {"skipped": w["status"]}
    row = _discovered(ctx, w["discovered_id"])
    disc = get_discoverer(row["platform"])
    media = disc.download_section(_item(row), w["start"], w["end"],
                                  _work_dir(ctx, row) / f"w{int(w['start'])}-{int(w['end'])}")
    settings = services.get_settings(ctx.conn)
    cache_file = ctx.cache.path("transcribe", f"win-{row['platform']}-{row['external_id']}-"
                                f"{w['start']}-{w['end']}-{settings['whisper_model']}.json")
    if not cache_file.exists():
        Cache.write_json(cache_file, ctx.transcriber(media, settings["whisper_model"],
                                                     settings["whisper_language"]))
    local_words = Cache.read_json(cache_file)["words"]
    # Temps de l'extrait -> temps de la vidéo complète.
    words = [{**wd, "start": wd["start"] + w["start"], "end": wd["end"] + w["start"]} for wd in local_words]
    with tx(ctx.conn):
        _set(ctx, "candidate_window", w["id"], media_path=str(media), words=json.dumps(words), status="ready")
    _maybe_score(ctx, row["id"])
    return {"words": len(words)}


def dead_prepare_window(ctx, payload: dict, error: str) -> None:
    w = ctx.conn.execute("SELECT discovered_id FROM candidate_window WHERE id = ?", (payload["window_id"],)).fetchone()
    if w is None:
        return
    with tx(ctx.conn):
        _set(ctx, "candidate_window", payload["window_id"], status="failed", error=error)
    _maybe_score(ctx, w["discovered_id"])


# -- ai_score : un seul appel par vidéo, sous budget --------------------------------------

def run_ai_score(ctx, payload: dict) -> dict:
    row = _discovered(ctx, payload["discovered_id"])
    if row["status"] not in ("scoring", "deferred"):
        return {"skipped": row["status"]}
    bot = services.get_bot(ctx.conn, row["bot_id"])
    ai = services.ai_settings(bot)
    clip_cfg = services.bot_settings(bot)
    windows = {w["id"]: w for w in ctx.conn.execute(
        "SELECT * FROM candidate_window WHERE discovered_id = ? AND status = 'ready'", (row["id"],))}

    # Candidats par fenêtre : un extrait qui démarre juste avant le moment fort
    # (pic du chat moins son retard), plus des découpes sur fins de phrase.
    segments: dict[str, tuple[Segment, sqlite3.Row, analyze.Candidate]] = {}
    anchors: dict[str, float] = {}
    signal = row["signal"] or "chat"
    for w in windows.values():
        words = json.loads(w["words"])
        cands: list[tuple[str, analyze.Candidate]] = []
        if w["peak"] is not None:
            delay = ai["chat_delay_seconds"] if signal == "chat" else 0
            anchor = max(w["start"], w["peak"] - delay)
            hook = analyze.hook_clip(words, anchor, window=(w["start"], w["end"]),
                                     min_seconds=clip_cfg["min_seconds"], max_seconds=clip_cfg["max_seconds"])
            if hook is not None:
                cands.append(("h", hook))
                anchors[f"w{w['id']}h"] = anchor
        for i, c in enumerate(analyze.find_clips(words, min_seconds=clip_cfg["min_seconds"],
                                                 max_seconds=clip_cfg["max_seconds"], max_clips=1)):
            cands.append((f"c{i}", c))
        for suffix, c in cands:
            seg = Segment(id=f"w{w['id']}{suffix}", start=round(c.start, 2), end=round(c.end, 2), text=c.text,
                          signal=signal, signal_score=w["signal_score"])
            segments[seg.id] = (seg, w, c)
    if not segments:
        with tx(ctx.conn):
            _set(ctx, "discovered", row["id"], status="done", error="aucun extrait exploitable",
                 updated_at=ctx.clock())
        return {"segments": 0}

    request_segments = [s for s, _, _ in segments.values()]
    provider = ctx.ai_override or make_provider(ai, dry_run=bool(bot["dry_run"]) or ctx.force_dry_run)
    if provider is None:
        scores = {s.id: SegmentScore(id=s.id, score=min(100, int(50 + 10 * s.signal_score)),
                                     reason=f"pic de {s.signal} (sans IA)", title=c.title or row["title"][:70],
                                     description=c.text[:300], hook="")
                  for s, _, c in segments.values()}
        origin, tokens = "signal", 0
    else:
        from .ai.base import ScoringRequest

        images = (segment_images(segments, anchors) if ai["vision"] and provider.supports_images else {})
        request = ScoringRequest(video_title=row["title"], channel=row["channel_id"] or "",
                                 segments=request_segments, images=images)
        outcome = score_with_budget(ctx.conn, provider, request, bot_id=bot["id"],
                                    daily_budget=ai["daily_token_budget"], now=ctx.clock())
        if outcome.scores is None:
            with tx(ctx.conn):
                _set(ctx, "discovered", row["id"], status="deferred", updated_at=ctx.clock())
            log.warning("bot %s : budget IA atteint, %s reporté", bot["id"], row["url"])
            return {"deferred": True}
        scores, origin, tokens = outcome.scores, "ai", outcome.tokens

    # Meilleurs segments, au-dessus du seuil, sans chevauchement.
    chosen: list[tuple[Segment, sqlite3.Row, SegmentScore]] = []
    for sid, sc in sorted(scores.items(), key=lambda kv: -kv[1].score):
        seg, w, _ = segments[sid]
        if sc.score < ai["min_score"] or len(chosen) >= ai["clips_per_video"]:
            continue
        if all(seg.end <= o.start or seg.start >= o.end for o, _, _ in chosen):
            chosen.append((seg, w, sc))

    now = ctx.clock()
    with tx(ctx.conn):
        for seg, w, sc in chosen:
            words = [wd for wd in json.loads(w["words"]) if wd["end"] > seg.start and wd["start"] < seg.end]
            cur = ctx.conn.execute(
                """INSERT OR IGNORE INTO clip (bot_id, source_id, start_ms, end_ms, score, title, description,
                       words, reason, origin, media_path, media_offset_ms, hook, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (bot["id"], row["source_id"], int(seg.start * 1000), int(seg.end * 1000), sc.score,
                 sc.title or seg.text[:70] or row["title"][:70], sc.description, json.dumps(words), sc.reason,
                 origin, w["media_path"], int(w["start"] * 1000), sc.hook.strip()[:60], now, now))
            if cur.rowcount:
                ctx.queue.enqueue("render", {"clip_id": cur.lastrowid}, key=f"render:{cur.lastrowid}")
        ctx.conn.execute("UPDATE candidate_window SET status = 'scored' WHERE discovered_id = ? AND status = 'ready'",
                         (row["id"],))
        _set(ctx, "discovered", row["id"], status="done", error=None, updated_at=now)
        if row["source_id"]:
            ctx.conn.execute("UPDATE source SET status = 'done' WHERE id = ?", (row["source_id"],))
    return {"segments": len(segments), "clips": len(chosen), "tokens": tokens}


MAX_IMAGES = 30  # par appel : ~9 000 tokens d'images au plus


def segment_images(segments: dict, anchors: dict[str, float]) -> dict[str, list[bytes]]:
    """Deux images par segment : peu après le début (l'accroche) et le moment fort
    (ou le milieu). Temps relatifs au fichier de la fenêtre téléchargée."""
    from .pipeline.layout import keyframe

    out: dict[str, list[bytes]] = {}
    budget = MAX_IMAGES
    # Les extraits ancrés sur un pic d'abord : ce sont les plus prometteurs.
    for sid in sorted(segments, key=lambda k: (not k.endswith("h"), k)):
        seg, w, _ = segments[sid]
        if budget < 2 or not w["media_path"]:
            continue
        moment = anchors.get(sid, (seg.start + seg.end) / 2)
        frames = [keyframe(Path(w["media_path"]), t - w["start"]) for t in (seg.start + 1.5, moment)]
        frames = [f for f in frames if f]
        if frames:
            out[sid] = frames
            budget -= len(frames)
    return out


def dead_discovered(ctx, payload: dict, error: str) -> None:
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE discovered SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                         (error, ctx.clock(), payload["discovered_id"]))


def resume_deferred(conn: sqlite3.Connection, queue, now: float) -> int:
    """Le lendemain d'un dépassement de budget : relance les analyses reportées."""
    day = today(now)
    rows = conn.execute("SELECT id, updated_at FROM discovered WHERE status = 'deferred'").fetchall()
    n = 0
    for r in rows:
        if today(r["updated_at"]) < day:
            queue.enqueue("ai_score", {"discovered_id": r["id"]}, key=f"ai_score:{r['id']}:{day}")
            n += 1
    return n


def enqueue_due_discoveries(conn: sqlite3.Connection, queue, now: float) -> int:
    n = 0
    for w in conn.execute("SELECT id, interval_minutes, last_checked_at FROM watch WHERE enabled = 1 AND dismissed = 0"):
        period = w["interval_minutes"] * 60
        if w["last_checked_at"] is None or w["last_checked_at"] + period <= now:
            queue.enqueue("discover", {"watch_id": w["id"]}, key=f"discover:{w['id']}:{int(now // period)}")
            n += 1
    return n


DISCOVERY_TASKS: dict[str, Task] = {
    "discover": Task(run_discover),
    "presort": Task(run_presort, on_dead=dead_discovered),
    "prepare_window": Task(run_prepare_window, on_dead=dead_prepare_window),
    "ai_score": Task(run_ai_score, on_dead=dead_discovered),
}
