"""Assemblage des composants (base, file, worker, planificateur) et démon."""

from __future__ import annotations

import logging
import threading
import time
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


class Daemon:
    """Worker + planificateur + API. Peut tourner au premier plan (`autoclip daemon`)
    ou dans un thread, à l'intérieur de la fenêtre (`autoclip app`)."""

    def __init__(self, home: Path | None = None, host: str = API_HOST, port: int = API_PORT,
                 tick_seconds: float = 30.0) -> None:
        import uvicorn

        from .api import create_app

        # Une connexion par thread : le worker et le planificateur ont chacun la leur.
        self.worker_rt = Runtime.open(home)
        self.scheduler_rt = Runtime.open(home)
        self.home = self.worker_rt.paths.home
        self.tick_seconds = tick_seconds
        self.stop_event = threading.Event()
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.home), host=host, port=port,
                                                    log_level="info"))
        self.url = f"http://{host}:{port}"
        self._threads: list[threading.Thread] = []

    def _scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                result = self.scheduler_rt.scheduler.tick()
                if any(result.values()):
                    log.info("planificateur : %s", result)
            except Exception:  # noqa: BLE001
                log.exception("erreur du planificateur")
            self.stop_event.wait(self.tick_seconds)

    def _chat_recorder(self) -> None:
        """Enregistre le chat des lives Twitch suivis (inactif sans compte Twitch connecté)."""
        from .discovery.twitch import ChatRecorder

        ChatRecorder(self.worker_rt.paths.db, self.stop_event).run()

    def _start_background(self) -> None:
        orphans = self.worker_rt.ctx.queue.requeue_orphans()
        if orphans:
            log.warning("%d job(s) interrompu(s) au dernier arrêt remis en file", orphans)
        self._threads = [
            threading.Thread(target=self.worker_rt.worker.run_forever, args=(self.stop_event,),
                             name="worker", daemon=True),
            threading.Thread(target=self._scheduler_loop, name="scheduler", daemon=True),
            threading.Thread(target=self._chat_recorder, name="twitch-chat", daemon=True),
        ]
        for t in self._threads:
            t.start()
        log.info("démon AutoClip prêt sur %s (données : %s)", self.url, self.home)

    def run(self) -> None:
        """Au premier plan, jusqu'à Ctrl+C / SIGTERM."""
        self._start_background()
        try:
            self.server.run()
        finally:
            self.shutdown()

    def start_in_thread(self, timeout: float = 15.0) -> None:
        self._start_background()
        thread = threading.Thread(target=self.server.run, name="api", daemon=True)
        thread.start()
        self._threads.append(thread)
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(f"impossible de démarrer l'API sur {self.url} (port déjà pris ?)")
            time.sleep(0.05)

    def shutdown(self) -> None:
        self.stop_event.set()
        self.server.should_exit = True
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=10)


def run_daemon(home: Path | None = None, host: str = API_HOST, port: int = API_PORT) -> None:
    Daemon(home, host, port).run()
