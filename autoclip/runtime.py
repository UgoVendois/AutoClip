"""Assemblage des composants (base, file, worker, planificateur) et démon."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from . import db
from .config import API_HOST, API_PORT, Paths, default_home
from .jobs.queue import JobQueue
from .jobs.worker import Worker
from .pipeline.cache import Cache
from .scheduler import Scheduler
from .tasks import LEASES, TASKS, Context

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    paths: Paths
    ctx: Context
    worker: Worker
    scheduler: Scheduler

    @classmethod
    def open(cls, home: Path | None = None) -> "Runtime":
        paths = Paths(home or default_home()).ensure()
        conn = db.connect(paths.db)
        queue = JobQueue(conn, lease_by_kind=LEASES)
        ctx = Context(conn=conn, queue=queue, cache=Cache(paths.cache))
        return cls(paths, ctx, Worker(ctx, queue, TASKS), Scheduler(conn, queue))


def run_daemon(home: Path | None = None, host: str = API_HOST, port: int = API_PORT,
               tick_seconds: float = 30.0) -> None:
    import uvicorn

    from .api import create_app

    # Une connexion par thread : le worker et le planificateur ont chacun la leur.
    worker_rt = Runtime.open(home)
    scheduler_rt = Runtime.open(home)
    orphans = worker_rt.ctx.queue.requeue_orphans()
    if orphans:
        log.warning("%d job(s) interrompu(s) au dernier arrêt remis en file", orphans)

    stop = threading.Event()

    def scheduler_loop() -> None:
        while not stop.is_set():
            try:
                result = scheduler_rt.scheduler.tick()
                if any(result.values()):
                    log.info("planificateur : %s", result)
            except Exception:  # noqa: BLE001
                log.exception("erreur du planificateur")
            stop.wait(tick_seconds)

    threads = [
        threading.Thread(target=worker_rt.worker.run_forever, args=(stop,), name="worker", daemon=True),
        threading.Thread(target=scheduler_loop, name="scheduler", daemon=True),
    ]
    for t in threads:
        t.start()
    log.info("démon AutoClip prêt sur http://%s:%d (données : %s)", host, port, worker_rt.paths.home)
    try:
        uvicorn.run(create_app(worker_rt.paths.home), host=host, port=port, log_level="info")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)
