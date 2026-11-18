"""Worker : prend les jobs de la file et les confie au bon handler."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .queue import Job, JobQueue, PermanentError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Task:
    run: Callable[[Any, dict[str, Any]], Any]
    # Appelé une seule fois quand le job échoue définitivement.
    on_dead: Callable[[Any, dict[str, Any], str], None] | None = None


class Worker:
    def __init__(self, ctx: Any, queue: JobQueue, tasks: dict[str, Task]) -> None:
        self.ctx = ctx
        self.queue = queue
        self.tasks = tasks

    def run_once(self) -> bool:
        """Traite un job. Renvoie False si aucun job n'était prêt."""
        job = self.queue.claim(self.tasks.keys())
        if job is None:
            return False
        self._execute(job)
        return True

    def _execute(self, job: Job) -> None:
        task = self.tasks[job.kind]
        log.info("job %s %s (essai %d/%d)", job.id, job.kind, job.attempts, job.max_attempts)
        try:
            result = task.run(self.ctx, job.payload)
        except PermanentError as exc:
            error, status = str(exc), self.queue.fail(job, str(exc), permanent=True)
        except Exception as exc:  # noqa: BLE001 : toute autre erreur est retentée
            error = f"{type(exc).__name__}: {exc}"
            log.exception("job %s %s en échec", job.id, job.kind)
            status = self.queue.fail(job, error)
        else:
            self.queue.complete(job, result)
            return
        log.warning("job %s %s -> %s : %s", job.id, job.kind, status, error)
        if status == "failed" and task.on_dead:
            task.on_dead(self.ctx, job.payload, error)

    def run_until_idle(self, limit: int = 100_000) -> int:
        done = 0
        while done < limit and self.run_once():
            done += 1
        return done

    def run_forever(self, stop: threading.Event, poll: float = 1.0) -> None:
        while not stop.is_set():
            try:
                busy = self.run_once()
            except Exception:  # noqa: BLE001 : la boucle ne doit jamais mourir
                log.exception("erreur dans la boucle du worker")
                busy = False
            if not busy:
                stop.wait(poll)
