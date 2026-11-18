"""Dry run de bout en bout : vidéo -> clips -> validation -> planning -> « publication ».

Tout s'exécute réellement (téléchargement, transcription, rendu, file de jobs,
planificateur) sauf l'envoi, remplacé par DryRunPublisher. Une horloge
virtuelle saute d'un créneau au suivant pour ne pas attendre des heures.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import db, services
from .config import Paths
from .jobs.queue import JobQueue
from .jobs.worker import Worker
from .pipeline.cache import Cache
from .pipeline.transcribe import Transcriber
from .scheduler import Scheduler
from .tasks import LEASES, TASKS, Context


class VirtualClock:
    def __init__(self, start: float | None = None) -> None:
        self.now = time.time() if start is None else start

    def __call__(self) -> float:
        return self.now

    def jump_to(self, t: float) -> None:
        self.now = max(self.now, t)


@dataclass
class DryRunReport:
    bot_id: int
    source_status: str
    source_error: str | None
    clips: list[dict]
    db_path: Path

    @property
    def ok(self) -> bool:
        return bool(self.clips) and all(c["status"] == "published" for c in self.clips)


def run(source: str, *, home: Path, every_hours: float = 4, days: str = "all",
        window: str = "09:00-21:00", timezone: str = "Europe/Paris", approve: bool = True,
        transcriber: Transcriber | None = None, max_clips: int | None = None) -> DryRunReport:
    paths = Paths(home).ensure()
    conn = db.connect(paths.db)
    clock = VirtualClock()
    queue = JobQueue(conn, clock=clock, lease_by_kind=LEASES, base_delay=5, max_delay=60)
    ctx = Context(conn=conn, queue=queue, cache=Cache(paths.cache), clock=clock, force_dry_run=True)
    if transcriber is not None:
        ctx.transcriber = transcriber
    worker = Worker(ctx, queue, TASKS)
    scheduler = Scheduler(conn, queue, clock=clock)

    clip_settings = {"max_clips": max_clips} if max_clips else None
    bot_id = services.create_bot(conn, f"dry run {Path(source).name}", dry_run=True,
                                 clip_settings=clip_settings)
    services.link_account(conn, bot_id, services.create_dry_account(conn))
    services.set_schedule(conn, bot_id, every_hours=every_hours, days=days, window=window,
                          timezone=timezone)
    source_id = services.add_source(conn, queue, bot_id, source)

    # 1. ingest -> transcribe -> analyze -> render : les clips arrivent en validation.
    _drain(worker, queue, clock)

    # 2. validation automatique puis publication au compte-gouttes.
    if approve:
        for clip in services.list_clips(conn, bot_id=bot_id, status="review"):
            services.approve_clip(conn, clip["id"], now=clock())
        for _ in range(10_000):
            scheduler.tick()
            worker.run_until_idle()
            wakeups = [t for t in (scheduler.next_due(), queue.next_wakeup()) if t is not None]
            if not wakeups:
                break
            clock.jump_to(min(wakeups))

    src = conn.execute("SELECT status, error FROM source WHERE id = ?", (source_id,)).fetchone()
    clips = [
        {
            "id": c["id"],
            "status": c["status"],
            "start": c["start_ms"] / 1000,
            "end": c["end_ms"] / 1000,
            "score": c["score"],
            "title": c["title"],
            "file": c["file_path"],
            "error": c["error"],
            "published_at": _published_at(conn, c["id"]),
        }
        for c in services.list_clips(conn, bot_id=bot_id)
    ]
    conn.close()
    return DryRunReport(bot_id, src["status"], src["error"], clips, paths.db)


def _drain(worker: Worker, queue: JobQueue, clock: VirtualClock) -> None:
    """Traite la file jusqu'au bout, en avançant l'horloge pendant les backoffs."""
    for _ in range(10_000):
        worker.run_until_idle()
        nxt = queue.next_wakeup()
        if nxt is None:
            return
        clock.jump_to(nxt)


def _published_at(conn, clip_id: int) -> float | None:
    row = conn.execute("SELECT MAX(scheduled_at) FROM publish_job WHERE clip_id = ? AND status = 'published'",
                       (clip_id,)).fetchone()
    return row[0]
