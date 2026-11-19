"""File de jobs persistante dans SQLite.

Garanties :
- idempotence : chaque job a une `idempotency_key` unique ; ré-enfiler la même
  clé renvoie le job existant au lieu d'en créer un second ;
- reprise après crash : un job pris est loué (`lease_until`). Si le worker meurt,
  le bail expire et le job est repris par le prochain `claim` ;
- retries : un échec ré-enfile le job avec un backoff exponentiel plafonné et
  du jitter, jusqu'à `max_attempts` ; une erreur permanente échoue tout de suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import socket
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..db import tx


class Cancelled(Exception):
    """L'utilisateur a annulé le travail en cours : le job se termine sans erreur."""


class PermanentError(Exception):
    """Erreur qu'aucun nouvel essai ne corrigera (fichier introuvable, requête refusée...)."""


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int  # essais commencés, celui-ci compris
    max_attempts: int
    idempotency_key: str


def default_key(kind: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    return f"{kind}:{digest}"


class JobQueue:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], float] = time.time,
        base_delay: float = 30.0,
        max_delay: float = 3600.0,
        lease_seconds: float = 900.0,
        lease_by_kind: dict[str, float] | None = None,
        rng: Callable[[], float] = random.random,
        worker_id: str | None = None,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.lease_seconds = lease_seconds
        self.lease_by_kind = lease_by_kind or {}
        self.rng = rng
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    # -- écriture -----------------------------------------------------------

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        key: str | None = None,
        max_attempts: int = 5,
        run_after: float | None = None,
    ) -> int:
        """Ajoute un job, ou renvoie l'id du job existant de même clé."""
        payload = payload or {}
        key = key or default_key(kind, payload)
        now = self.clock()
        with tx(self.conn):
            self.conn.execute(
                """INSERT INTO job (kind, payload, idempotency_key, max_attempts, run_after,
                                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (idempotency_key) DO NOTHING""",
                (kind, json.dumps(payload), key, max_attempts,
                 now if run_after is None else run_after, now, now),
            )
            row = self.conn.execute("SELECT id FROM job WHERE idempotency_key = ?", (key,)).fetchone()
        return row["id"]

    def claim(self, kinds: Iterable[str] | None = None) -> Job | None:
        """Prend le prochain job prêt (ou dont le bail a expiré) et le loue."""
        kinds = list(kinds or [])
        now = self.clock()
        query = """SELECT * FROM job
                   WHERE ((status = 'queued' AND run_after <= ?)
                       OR (status = 'running' AND lease_until < ?))"""
        params: list[Any] = [now, now]
        if kinds:
            query += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        query += " ORDER BY run_after, id LIMIT 1"

        with tx(self.conn):
            while True:
                row = self.conn.execute(query, params).fetchone()
                if row is None:
                    return None
                if row["status"] == "running" and row["attempts"] >= row["max_attempts"]:
                    # Le worker est mort pendant le dernier essai autorisé.
                    self.conn.execute(
                        """UPDATE job SET status = 'failed', lease_until = NULL, updated_at = ?,
                                          last_error = COALESCE(last_error, 'bail expiré au dernier essai')
                           WHERE id = ?""",
                        (now, row["id"]),
                    )
                    continue
                lease = self.lease_by_kind.get(row["kind"], self.lease_seconds)
                self.conn.execute(
                    """UPDATE job SET status = 'running', attempts = attempts + 1,
                                      lease_until = ?, worker = ?, updated_at = ?, started_at = ?
                       WHERE id = ?""",
                    (now + lease, self.worker_id, now, now, row["id"]),
                )
                return Job(
                    id=row["id"],
                    kind=row["kind"],
                    payload=json.loads(row["payload"]),
                    attempts=row["attempts"] + 1,
                    max_attempts=row["max_attempts"],
                    idempotency_key=row["idempotency_key"],
                )

    def complete(self, job: Job, result: Any = None) -> bool:
        """Marque le job terminé. Renvoie False si le bail a été repris entre-temps."""
        now = self.clock()
        with tx(self.conn):
            cur = self.conn.execute(
                """UPDATE job SET status = 'done', lease_until = NULL, result = ?, updated_at = ?
                   WHERE id = ? AND status = 'running' AND attempts = ?""",
                (json.dumps(result), now, job.id, job.attempts),
            )
        return cur.rowcount == 1

    def fail(self, job: Job, error: str, *, permanent: bool = False) -> str:
        """Enregistre un échec. Renvoie le nouveau statut : 'queued', 'failed' ou
        'stale' si un autre worker a repris le job."""
        now = self.clock()
        final = permanent or job.attempts >= job.max_attempts
        status = "failed" if final else "queued"
        run_after = now if final else now + self.backoff(job.attempts)
        with tx(self.conn):
            cur = self.conn.execute(
                """UPDATE job SET status = ?, run_after = ?, lease_until = NULL,
                                  last_error = ?, updated_at = ?
                   WHERE id = ? AND status = 'running' AND attempts = ?""",
                (status, run_after, error[:4000], now, job.id, job.attempts),
            )
        return status if cur.rowcount == 1 else "stale"

    def heartbeat(self, job: Job) -> bool:
        """Prolonge le bail d'un job long."""
        now = self.clock()
        lease = self.lease_by_kind.get(job.kind, self.lease_seconds)
        with tx(self.conn):
            cur = self.conn.execute(
                "UPDATE job SET lease_until = ? WHERE id = ? AND status = 'running' AND attempts = ?",
                (now + lease, job.id, job.attempts),
            )
        return cur.rowcount == 1

    def requeue_orphans(self) -> int:
        """Au démarrage du démon (instance unique) : les jobs encore 'running'
        appartenaient au processus précédent, on les remet en file."""
        now = self.clock()
        with tx(self.conn):
            cur = self.conn.execute(
                """UPDATE job SET status = 'queued', run_after = ?, lease_until = NULL, updated_at = ?
                   WHERE status = 'running'""",
                (now, now),
            )
        return cur.rowcount

    def retry(self, job_id: int) -> bool:
        """Relance à la main un job échoué (remet le compteur d'essais à zéro)."""
        now = self.clock()
        with tx(self.conn):
            cur = self.conn.execute(
                """UPDATE job SET status = 'queued', attempts = 0, run_after = ?, updated_at = ?
                   WHERE id = ? AND status = 'failed'""",
                (now, now, job_id),
            )
        return cur.rowcount == 1

    # -- lecture ------------------------------------------------------------

    def backoff(self, attempt: int) -> float:
        """Délai avant l'essai suivant : exponentiel, plafonné, avec « equal jitter »
        (entre la moitié et la totalité du délai nominal)."""
        nominal = min(self.max_delay, self.base_delay * 2 ** max(0, attempt - 1))
        return nominal / 2 + self.rng() * nominal / 2

    def get(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM job GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def next_wakeup(self) -> float | None:
        """Prochain instant où un job deviendra prêt (file ou bail expiré)."""
        row = self.conn.execute(
            """SELECT MIN(t) FROM (
                   SELECT MIN(run_after) AS t FROM job WHERE status = 'queued'
                   UNION ALL
                   SELECT MIN(lease_until) FROM job WHERE status = 'running')"""
        ).fetchone()
        return row[0]
