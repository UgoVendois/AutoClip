"""Planificateur : publie au compte-gouttes les clips approuvés.

Règles d'un planning (table `schedule`) :
- au plus une publication toutes les `every_hours` heures ;
- uniquement les jours cochés dans `weekdays` (lundi = bit 0) ;
- uniquement dans la plage horaire [window_start, window_end[ du fuseau
  `timezone` (plage qui peut passer minuit ; bornes égales = toute la journée).

Un seul créneau est réservé à l'avance par bot : quand il est consommé, le
clip approuvé suivant (ordre d'approbation) reçoit le créneau d'après. Toutes
les opérations sont idempotentes : appeler `tick` plusieurs fois ne crée ni
doublon ni saut de créneau.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .db import tx
from .jobs.queue import JobQueue

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ALL_DAYS = 0b1111111


def parse_hhmm(value: str) -> dtime:
    hours, minutes = value.strip().split(":")
    return dtime(int(hours), int(minutes))


def weekday_mask(days: str | list[str]) -> int:
    """'mon,wed,fri' ou ['mon', 'wed'] -> masque de bits. 'all' = tous les jours."""
    if isinstance(days, str):
        days = [d for d in days.replace(" ", "").lower().split(",") if d]
    if not days or days == ["all"]:
        return ALL_DAYS
    mask = 0
    for day in days:
        if day not in DAYS:
            raise ValueError(f"jour inconnu : {day!r} (attendu : {', '.join(DAYS)})")
        mask |= 1 << DAYS.index(day)
    return mask


@dataclass(frozen=True)
class ScheduleSpec:
    every_hours: float
    weekdays: int = ALL_DAYS
    window_start: dtime = dtime(0, 0)
    window_end: dtime = dtime(0, 0)
    tz: ZoneInfo = ZoneInfo("UTC")

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ScheduleSpec":
        return cls(
            every_hours=row["every_hours"],
            weekdays=row["weekdays"],
            window_start=parse_hhmm(row["window_start"]),
            window_end=parse_hhmm(row["window_end"]),
            tz=ZoneInfo(row["timezone"]),
        )


def next_slot(spec: ScheduleSpec, now: float, last_slot: float | None) -> float:
    """Premier instant >= now, espacé d'au moins every_hours du créneau précédent,
    qui tombe un jour autorisé et dans la plage horaire."""
    if not spec.weekdays & ALL_DAYS:
        raise ValueError("aucun jour autorisé dans le planning")
    earliest = now if last_slot is None else max(now, last_slot + spec.every_hours * 3600)
    local = datetime.fromtimestamp(earliest, spec.tz)
    # On part de la veille : une plage qui passe minuit peut couvrir le petit matin.
    for offset in range(-1, 9):
        day = local.date() + timedelta(days=offset)
        if not spec.weekdays >> day.weekday() & 1:
            continue
        opens = datetime.combine(day, spec.window_start, spec.tz).timestamp()
        end_day = day if spec.window_end > spec.window_start else day + timedelta(days=1)
        closes = datetime.combine(end_day, spec.window_end, spec.tz).timestamp()
        candidate = max(earliest, opens)
        if candidate < closes:
            return candidate
    raise AssertionError("inatteignable : une semaine contient toujours un jour autorisé")


class Scheduler:
    def __init__(self, conn: sqlite3.Connection, queue: JobQueue,
                 clock: Callable[[], float] = time.time) -> None:
        self.conn = conn
        self.queue = queue
        self.clock = clock

    def tick(self) -> dict[str, int]:
        """Réserve le prochain créneau de chaque bot puis envoie dans la file de
        jobs les publications arrivées à échéance."""
        now = self.clock()
        planned = dispatched = 0
        with tx(self.conn):
            schedules = self.conn.execute("SELECT * FROM schedule WHERE enabled = 1").fetchall()
            for sched in schedules:
                # Un envoi libère le créneau réservé : on réserve aussitôt le suivant.
                # Pas de rafale après une longue absence : chaque créneau est espacé
                # du précédent (last_slot_at), donc au plus un envoi par tour.
                while True:
                    p = self._plan(sched["bot_id"], now)
                    d = self._dispatch(sched["bot_id"], now)
                    planned, dispatched = planned + p, dispatched + d
                    if not d:
                        break
        return {"planned": planned, "dispatched": dispatched}

    def _plan(self, bot_id: int, now: float) -> int:
        sched = self.conn.execute("SELECT * FROM schedule WHERE bot_id = ?", (bot_id,)).fetchone()
        upcoming = self.conn.execute(
            """SELECT 1 FROM publish_job pj JOIN clip c ON c.id = pj.clip_id
               WHERE c.bot_id = ? AND pj.status = 'pending' AND pj.scheduled_at IS NOT NULL
               LIMIT 1""",
            (bot_id,),
        ).fetchone()
        if upcoming:
            return 0
        clip = self.conn.execute(
            """SELECT c.id FROM clip c
               WHERE c.bot_id = ? AND c.status = 'scheduled'
                 AND EXISTS (SELECT 1 FROM publish_job pj
                             WHERE pj.clip_id = c.id AND pj.status = 'pending'
                               AND pj.scheduled_at IS NULL)
               ORDER BY c.approved_at, c.id LIMIT 1""",
            (bot_id,),
        ).fetchone()
        if clip is None:
            return 0
        slot = next_slot(ScheduleSpec.from_row(sched), now, sched["last_slot_at"])
        self.conn.execute(
            """UPDATE publish_job SET scheduled_at = ?, updated_at = ?
               WHERE clip_id = ? AND status = 'pending' AND scheduled_at IS NULL""",
            (slot, now, clip["id"]),
        )
        self.conn.execute("UPDATE schedule SET last_slot_at = ? WHERE bot_id = ?", (slot, bot_id))
        return 1

    def _dispatch(self, bot_id: int, now: float) -> int:
        due = self.conn.execute(
            """SELECT pj.id FROM publish_job pj JOIN clip c ON c.id = pj.clip_id
               WHERE c.bot_id = ? AND pj.status = 'pending' AND pj.scheduled_at <= ?""",
            (bot_id, now),
        ).fetchall()
        for row in due:
            self.queue.enqueue("publish", {"publish_job_id": row["id"]}, key=f"publish:{row['id']}")
            self.conn.execute(
                "UPDATE publish_job SET status = 'queued', updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
        if due:
            # Envoi en retard (démon arrêté) : l'espacement part de l'envoi réel.
            self.conn.execute(
                "UPDATE schedule SET last_slot_at = MAX(COALESCE(last_slot_at, 0), ?) WHERE bot_id = ?",
                (now, bot_id),
            )
        return len(due)

    def next_due(self) -> float | None:
        """Prochain créneau réservé (utile pour avancer une horloge virtuelle)."""
        row = self.conn.execute(
            "SELECT MIN(scheduled_at) FROM publish_job WHERE status = 'pending'"
        ).fetchone()
        return row[0]

    def has_unplanned(self) -> bool:
        return self.conn.execute(
            """SELECT 1 FROM publish_job pj JOIN clip c ON c.id = pj.clip_id
               JOIN schedule s ON s.bot_id = c.bot_id AND s.enabled = 1
               WHERE pj.status = 'pending' AND pj.scheduled_at IS NULL LIMIT 1"""
        ).fetchone() is not None
