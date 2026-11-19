"""Handlers des jobs : une fonction par étape du pipeline.

Chaque handler est idempotent : relancé après un crash, il réutilise ce qui
est déjà en cache ou en base et ré-enfile l'étape suivante avec une clé fixe.
"""

from __future__ import annotations

import inspect
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import services
from .db import tx
from .jobs.queue import Cancelled, JobQueue, PermanentError
from .jobs.worker import Task
from .pipeline import analyze, ingest, render
from .pipeline import layout as lay
from .pipeline.cache import Cache, sha256_file, short_hash
from .pipeline.transcribe import FasterWhisperTranscriber, Transcriber
from .publishers import Account, PublishRequest, Publisher, publisher_for_platform
from .publishers.dryrun import DryRunPublisher

log = logging.getLogger(__name__)

LEASES = {"ingest": 3 * 3600, "transcribe": 6 * 3600, "analyze": 600, "render": 3600,
          "publish": 3600, "discover": 600, "presort": 2 * 3600, "prepare_window": 3600,
          "ai_score": 1800, "identity_generate": 900, "identity_images": 1800,
          "identity_apply": 1800, "playlist_add": 600, "scout": 900}


@dataclass
class Context:
    conn: sqlite3.Connection
    queue: JobQueue
    cache: Cache
    clock: Callable[[], float] = time.time
    transcriber: Transcriber = field(default_factory=FasterWhisperTranscriber)
    force_dry_run: bool = False
    publisher_override: Callable[[str], Publisher] | None = None
    ai_override: object | None = None  # AIProvider imposé (tests)
    image_override: object | None = None  # ImageGenerator imposé (tests)
    channel_override: Callable[[str], object] | None = None  # ChannelManager par plateforme

    def publisher(self, dry_run: bool, platform: str) -> Publisher:
        if dry_run or self.force_dry_run:
            return DryRunPublisher(self.cache)
        if self.publisher_override:
            return self.publisher_override(platform)
        return publisher_for_platform(platform)


def _source(ctx: Context, source_id: int) -> sqlite3.Row:
    row = ctx.conn.execute("SELECT * FROM source WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise PermanentError(f"source {source_id} supprimée")
    return row


def _set_source(ctx: Context, source_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    ctx.conn.execute(f"UPDATE source SET {sets} WHERE id = ?", (*fields.values(), source_id))


# -- ingest ---------------------------------------------------------------------

def _source_stop(ctx: Context, source_id: int):
    def should_stop() -> bool:
        return services.is_cancelled(ctx.conn.execute("SELECT error FROM source WHERE id = ?",
                                                      (source_id,)).fetchone())
    return should_stop


def run_ingest(ctx: Context, payload: dict) -> dict:
    src = _source(ctx, payload["source_id"])
    if services.is_cancelled(src):
        return {"cancelled": True}
    if src["media_path"] and Path(src["media_path"]).is_file() and src["sha256"]:
        path, digest = Path(src["media_path"]), src["sha256"]
    else:
        if src["kind"] == "file":
            path = ingest.resolve_local(src["uri"])
        else:
            try:
                path = ingest.download(src["uri"], _download_dir(ctx, src["uri"]),
                                       should_stop=_source_stop(ctx, src["id"]))
            except Cancelled:
                return {"cancelled": True}
        digest = sha256_file(path)
    with tx(ctx.conn):
        if services.is_cancelled(_source(ctx, src["id"])):
            return {"cancelled": True}
        _set_source(ctx, src["id"], media_path=str(path), sha256=digest, status="transcribe", error=None)
        ctx.queue.enqueue("transcribe", {"source_id": src["id"]}, key=f"transcribe:{src['id']}")
    return {"media": str(path)}


def _download_dir(ctx: Context, uri: str) -> Path:
    directory = ctx.cache.root / "ingest" / short_hash(uri)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# -- transcribe -----------------------------------------------------------------

def transcript_path(ctx: Context, src: sqlite3.Row) -> Path:
    settings = services.get_settings(ctx.conn)
    name = f"{src['sha256']}-{settings['whisper_model']}-{settings['whisper_language'] or 'auto'}.json"
    return ctx.cache.path("transcribe", name)


def _transcribe(ctx: Context, media: Path, model: str, language: str | None, should_stop) -> dict:
    """Appelle le transcripteur, avec l'arrêt sur annulation s'il le prend en charge."""
    try:
        accepts = "should_stop" in inspect.signature(ctx.transcriber).parameters
    except (TypeError, ValueError):
        accepts = False
    if accepts:
        return ctx.transcriber(media, model, language, should_stop=should_stop)
    return ctx.transcriber(media, model, language)


def run_transcribe(ctx: Context, payload: dict) -> dict:
    src = _source(ctx, payload["source_id"])
    if services.is_cancelled(src):
        return {"cancelled": True}
    path = transcript_path(ctx, src)
    if not path.exists():
        settings = services.get_settings(ctx.conn)
        log.info("transcription de %s (modèle %s)", src["media_path"], settings["whisper_model"])
        try:
            data = _transcribe(ctx, Path(src["media_path"]), settings["whisper_model"],
                               settings["whisper_language"], _source_stop(ctx, src["id"]))
        except Cancelled:
            return {"cancelled": True}
        Cache.write_json(path, data)
    with tx(ctx.conn):
        if services.is_cancelled(_source(ctx, src["id"])):
            return {"cancelled": True}
        _set_source(ctx, src["id"], status="analyze")
        ctx.queue.enqueue("analyze", {"source_id": src["id"]}, key=f"analyze:{src['id']}")
    return {"transcript": str(path)}


# -- analyze --------------------------------------------------------------------

def run_analyze(ctx: Context, payload: dict) -> dict:
    src = _source(ctx, payload["source_id"])
    if services.is_cancelled(src):
        return {"cancelled": True}
    bot = services.get_bot(ctx.conn, src["bot_id"])
    settings = services.bot_settings(bot)
    transcript = Cache.read_json(transcript_path(ctx, src))
    words = transcript["words"]
    candidates = analyze.find_clips(words, min_seconds=settings["min_seconds"],
                                    max_seconds=settings["max_seconds"],
                                    max_clips=settings["max_clips"])
    now = ctx.clock()
    with tx(ctx.conn):
        for cand in candidates:
            ctx.conn.execute(
                """INSERT OR IGNORE INTO clip (bot_id, source_id, start_ms, end_ms, score, title,
                                               description, words, language, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (bot["id"], src["id"], int(cand.start * 1000), int(cand.end * 1000), cand.score,
                 cand.title, cand.text, json.dumps(cand.words), transcript.get("language"), now, now),
            )
        clip_ids = [r["id"] for r in ctx.conn.execute(
            "SELECT id FROM clip WHERE source_id = ? AND status = 'render'", (src["id"],))]
        for clip_id in clip_ids:
            ctx.queue.enqueue("render", {"clip_id": clip_id}, key=f"render:{clip_id}")
        _set_source(ctx, src["id"], status="done",
                    error=None if candidates else "aucun extrait assez long trouvé")
    return {"clips": len(candidates)}


# -- render ---------------------------------------------------------------------

def run_render(ctx: Context, payload: dict) -> dict:
    clip = ctx.conn.execute(
        "SELECT c.*, s.media_path AS source_media FROM clip c JOIN source s ON s.id = c.source_id WHERE c.id = ?",
        (payload["clip_id"],),
    ).fetchone()
    if clip is None:
        raise PermanentError(f"clip {payload['clip_id']} supprimé")
    if clip["status"] not in ("render", "failed"):  # 'failed' : relance manuelle
        return {"skipped": clip["status"]}
    settings = services.bot_settings(services.get_bot(ctx.conn, clip["bot_id"]))
    # Clip découvert : le média est l'extrait téléchargé, qui commence à media_offset.
    media = clip["media_path"] or clip["source_media"]
    if not media:
        raise PermanentError(f"clip {clip['id']} : aucun média source")
    offset = clip["media_offset_ms"] / 1000
    start, end = clip["start_ms"] / 1000 - offset, clip["end_ms"] / 1000 - offset
    layout, facecam = lay.choose(settings["layout"], Path(media), start, end,
                                 ctx.cache.path("layout", f"{short_hash(media, start, end)}.json"))
    hook = clip["hook"] if settings["hook_text"] else ""
    style = {k: settings[k] for k in ("subtitle_words", "font", "font_size", "trim_silences", "hook_seconds")}
    style.update(layout=layout, facecam=facecam and [facecam.x, facecam.y, facecam.w, facecam.h], hook=hook)
    out = ctx.cache.path("render", f"clip-{clip['id']}-{short_hash(clip['start_ms'], clip['end_ms'], style)}.mp4")
    words = [{**w, "start": w["start"] - offset, "end": w["end"] - offset} for w in json.loads(clip["words"])]
    render.render_clip(Path(media), start, end, words, out, group_size=style["subtitle_words"],
                       font=style["font"], font_size=style["font_size"], layout=layout, facecam=facecam,
                       trim_silences=settings["trim_silences"], hook=hook, hook_seconds=settings["hook_seconds"])
    with tx(ctx.conn):
        ctx.conn.execute(
            "UPDATE clip SET file_path = ?, status = 'review', error = NULL, updated_at = ? "
            "WHERE id = ? AND status IN ('render', 'failed')",
            (str(out), ctx.clock(), clip["id"]),
        )
    # Premier montage seulement : une accroche modifiée à la main relance le rendu d'un clip en relecture.
    if settings.get("auto_approve") and clip["status"] == "render" and not clip["file_path"]:
        try:  # validation automatique (sans relecture) : publié selon le planning
            services.approve_clip(ctx.conn, clip["id"], now=ctx.clock())
        except services.ServiceError as exc:  # aucun compte lié : le clip attend une validation
            log.info("clip %s non validé automatiquement : %s", clip["id"], exc)
    return {"file": str(out)}


# -- publish --------------------------------------------------------------------

def _publish_row(ctx: Context, publish_job_id: int) -> sqlite3.Row:
    row = ctx.conn.execute(
        """SELECT pj.*, c.title, c.description, c.file_path, c.bot_id, c.language, b.dry_run, b.clip_settings,
                  a.platform, a.display_name, a.keyring_ref
           FROM publish_job pj
           JOIN clip c ON c.id = pj.clip_id
           JOIN bot b ON b.id = c.bot_id
           JOIN account a ON a.id = pj.account_id
           WHERE pj.id = ?""",
        (publish_job_id,),
    ).fetchone()
    if row is None:
        raise PermanentError(f"publication {publish_job_id} supprimée")
    return row


def run_publish(ctx: Context, payload: dict) -> dict:
    row = _publish_row(ctx, payload["publish_job_id"])
    if row["status"] == "published":
        return {"external_id": row["external_id"]}
    now = ctx.clock()
    with tx(ctx.conn):
        ctx.conn.execute(
            "UPDATE publish_job SET status = 'publishing', attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (now, row["id"]))
        ctx.conn.execute("UPDATE clip SET status = 'publishing', updated_at = ? WHERE id = ?",
                         (now, row["clip_id"]))
    account = Account(row["account_id"], row["platform"], row["display_name"], row["keyring_ref"])
    settings = services.get_settings(ctx.conn)
    tags = json.loads(row["clip_settings"]).get("tags", [])
    request = PublishRequest(
        video_path=Path(row["file_path"]),
        title=row["title"],
        description=row["description"],
        token=f"c{row['clip_id']}a{row['account_id']}",
        tags=list(tags),
        privacy=settings["youtube_privacy"],
        language=row["language"] or "",
    )
    try:
        result = ctx.publisher(bool(row["dry_run"]), row["platform"]).publish_once(request, account)
    except Exception as exc:
        with tx(ctx.conn):
            ctx.conn.execute(
                "UPDATE publish_job SET status = 'queued', last_error = ?, updated_at = ? WHERE id = ?",
                (f"{type(exc).__name__}: {exc}", ctx.clock(), row["id"]))
        raise
    with tx(ctx.conn):
        now = ctx.clock()
        ctx.conn.execute(
            """UPDATE publish_job SET status = 'published', external_id = ?, url = ?,
                                      last_error = NULL, updated_at = ? WHERE id = ?""",
            (result.external_id, result.url, now, row["id"]))
        remaining = ctx.conn.execute(
            "SELECT COUNT(*) FROM publish_job WHERE clip_id = ? AND status != 'published'",
            (row["clip_id"],)).fetchone()[0]
        if remaining == 0:
            ctx.conn.execute("UPDATE clip SET status = 'published', updated_at = ? WHERE id = ?",
                             (now, row["clip_id"]))
        has_playlists = ctx.conn.execute(
            "SELECT 1 FROM playlist WHERE bot_id = ? AND account_id = ? AND external_id IS NOT NULL",
            (row["bot_id"], row["account_id"])).fetchone()
        if has_playlists:
            ctx.queue.enqueue("playlist_add", {"publish_job_id": row["id"]}, key=f"playlist_add:{row['id']}")
    return {"external_id": result.external_id, "url": result.url}


# -- échecs définitifs ------------------------------------------------------------

def dead_source(ctx: Context, payload: dict, error: str) -> None:
    with tx(ctx.conn):
        _set_source(ctx, payload["source_id"], status="failed", error=error)


def dead_render(ctx: Context, payload: dict, error: str) -> None:
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE clip SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                         (error, ctx.clock(), payload["clip_id"]))


def dead_publish(ctx: Context, payload: dict, error: str) -> None:
    with tx(ctx.conn):
        row = ctx.conn.execute("SELECT clip_id FROM publish_job WHERE id = ?",
                               (payload["publish_job_id"],)).fetchone()
        if row is None:
            return
        now = ctx.clock()
        ctx.conn.execute("UPDATE publish_job SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
                         (error, now, payload["publish_job_id"]))
        ctx.conn.execute("UPDATE clip SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                         (error, now, row["clip_id"]))


def _discovery_tasks() -> dict[str, Task]:
    from .discovery_tasks import DISCOVERY_TASKS
    from .identity import IDENTITY_TASKS
    from .scout import SCOUT_TASKS

    return {**DISCOVERY_TASKS, **IDENTITY_TASKS, **SCOUT_TASKS}


TASKS: dict[str, Task] = {
    "ingest": Task(run_ingest, on_dead=dead_source),
    "transcribe": Task(run_transcribe, on_dead=dead_source),
    "analyze": Task(run_analyze, on_dead=dead_source),
    "render": Task(run_render, on_dead=dead_render),
    "publish": Task(run_publish, on_dead=dead_publish),
}
TASKS.update(_discovery_tasks())
