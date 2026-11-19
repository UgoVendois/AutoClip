import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from autoclip import db, services
from autoclip.jobs.queue import JobQueue
from autoclip.scheduler import ScheduleSpec, Scheduler, next_slot, parse_hhmm, weekday_mask

from .helpers import FakeClock

PARIS = ZoneInfo("Europe/Paris")
H = 3600


def ts(text: str, tz=PARIS) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=tz).timestamp()


def spec(every=4, days="all", window=("09:00", "21:00"), tz=PARIS):
    return ScheduleSpec(every_hours=every, weekdays=weekday_mask(days),
                        window_start=parse_hhmm(window[0]), window_end=parse_hhmm(window[1]), tz=tz)


# 2026-11-16 est un lundi.
class TestNextSlot(unittest.TestCase):
    def test_now_inside_window(self):
        self.assertEqual(next_slot(spec(), ts("2026-11-16 10:30"), None), ts("2026-11-16 10:30"))

    def test_before_window_waits_for_opening(self):
        self.assertEqual(next_slot(spec(), ts("2026-11-16 06:00"), None), ts("2026-11-16 09:00"))

    def test_after_window_goes_to_next_day(self):
        self.assertEqual(next_slot(spec(), ts("2026-11-16 22:00"), None), ts("2026-11-17 09:00"))

    def test_window_end_is_exclusive(self):
        self.assertEqual(next_slot(spec(), ts("2026-11-16 21:00"), None), ts("2026-11-17 09:00"))

    def test_spacing_from_last_slot(self):
        self.assertEqual(next_slot(spec(every=4), ts("2026-11-16 10:00"), ts("2026-11-16 09:00")),
                         ts("2026-11-16 13:00"))

    def test_spacing_pushes_past_window(self):
        self.assertEqual(next_slot(spec(every=4), ts("2026-11-16 18:00"), ts("2026-11-16 18:00")),
                         ts("2026-11-17 09:00"))

    def test_old_last_slot_does_not_delay(self):
        self.assertEqual(next_slot(spec(), ts("2026-11-16 12:00"), ts("2026-11-10 12:00")),
                         ts("2026-11-16 12:00"))

    def test_skips_disallowed_days(self):
        # Vendredi soir -> le prochain jour autorisé (lun, mer) est lundi.
        self.assertEqual(next_slot(spec(days="mon,wed"), ts("2026-11-20 22:00"), None),
                         ts("2026-11-23 09:00"))

    def test_window_crossing_midnight(self):
        s = spec(window=("22:00", "02:00"))
        self.assertEqual(next_slot(s, ts("2026-11-16 23:30"), None), ts("2026-11-16 23:30"))
        self.assertEqual(next_slot(s, ts("2026-11-17 01:00"), None), ts("2026-11-17 01:00"))
        self.assertEqual(next_slot(s, ts("2026-11-17 03:00"), None), ts("2026-11-17 22:00"))

    def test_midnight_window_respects_weekday_of_opening(self):
        # Seul lundi est autorisé : la plage lun 22h -> mar 2h couvre mardi 1h.
        s = spec(days="mon", window=("22:00", "02:00"))
        self.assertEqual(next_slot(s, ts("2026-11-17 01:00"), None), ts("2026-11-17 01:00"))
        self.assertEqual(next_slot(s, ts("2026-11-17 03:00"), None), ts("2026-11-23 22:00"))

    def test_equal_bounds_mean_all_day(self):
        s = spec(window=("00:00", "00:00"))
        self.assertEqual(next_slot(s, ts("2026-11-16 03:17"), None), ts("2026-11-16 03:17"))

    def test_dst_change(self):
        # Passage à l'heure d'hiver le 2026-10-25 : 9h reste 9h heure locale.
        self.assertEqual(next_slot(spec(), ts("2026-10-24 22:00"), None), ts("2026-10-25 09:00"))
        self.assertEqual(ts("2026-10-25 09:00") - ts("2026-10-24 09:00"), 25 * H)

    def test_weekday_mask_parsing(self):
        self.assertEqual(weekday_mask("mon,sun"), 0b1000001)
        self.assertEqual(weekday_mask("all"), 0b1111111)
        with self.assertRaises(ValueError):
            weekday_mask("lundi")


class SchedulerTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.clock = FakeClock(ts("2026-11-16 08:00"))
        self.queue = JobQueue(self.conn, clock=self.clock)
        self.sched = Scheduler(self.conn, self.queue, clock=self.clock)
        c = self.conn
        c.execute("INSERT INTO bot (id, name, created_at) VALUES (1, 'b', 0)")
        c.execute("INSERT INTO source (id, bot_id, kind, uri, created_at) VALUES (1, 1, 'file', 'x', 0)")
        c.execute("INSERT INTO account (id, platform, display_name, created_at) VALUES (1, 'youtube', 'a', 0)")
        c.execute("INSERT INTO account (id, platform, display_name, created_at) VALUES (2, 'youtube', 'b', 0)")
        c.execute("""INSERT INTO schedule (bot_id, every_hours, weekdays, window_start, window_end, timezone)
                     VALUES (1, 4, 127, '09:00', '21:00', 'Europe/Paris')""")

    def approve(self, clip_id, accounts=(1,), approved_at=0.0):
        self.conn.execute(
            """INSERT INTO clip (id, bot_id, source_id, start_ms, end_ms, status, approved_at,
                                 created_at, updated_at)
               VALUES (?, 1, 1, ?, ?, 'scheduled', ?, 0, 0)""",
            (clip_id, clip_id * 1000, clip_id * 1000 + 500, approved_at))
        for acc in accounts:
            self.conn.execute(
                "INSERT INTO publish_job (clip_id, account_id, created_at, updated_at) VALUES (?, ?, 0, 0)",
                (clip_id, acc))

    def slots(self):
        rows = self.conn.execute("SELECT clip_id, scheduled_at, status FROM publish_job ORDER BY id")
        return [(r["clip_id"], r["scheduled_at"], r["status"]) for r in rows]

    def publish_jobs_in_queue(self):
        return [r["idempotency_key"] for r in
                self.conn.execute("SELECT idempotency_key FROM job WHERE kind = 'publish' ORDER BY id")]


class TestSchedulerTick(SchedulerTestCase):
    def test_reserves_one_slot_at_a_time_in_approval_order(self):
        self.approve(2, approved_at=10)
        self.approve(1, approved_at=20)
        self.assertEqual(self.sched.tick(), {"planned": 1, "dispatched": 0})
        self.assertEqual(self.slots(), [(2, ts("2026-11-16 09:00"), "pending"), (1, None, "pending")])
        self.assertEqual(self.sched.tick(), {"planned": 0, "dispatched": 0})

    def test_dispatches_when_due_then_plans_next(self):
        self.approve(1, approved_at=1)
        self.approve(2, approved_at=2)
        self.sched.tick()
        self.clock.set(ts("2026-11-16 09:00"))
        # Envoi du clip 1 et, dans le même tour, réservation du créneau suivant (9h + 4h).
        self.assertEqual(self.sched.tick(), {"planned": 1, "dispatched": 1})
        self.assertEqual(self.publish_jobs_in_queue(), ["publish:1"])
        self.assertEqual(self.slots()[1], (2, ts("2026-11-16 13:00"), "pending"))
        self.assertEqual(self.sched.tick(), {"planned": 0, "dispatched": 0})

    def test_no_burst_after_long_downtime(self):
        for i in range(1, 4):
            self.approve(i, approved_at=i)
        self.sched.tick()  # clip 1 réservé à 9h
        self.clock.set(ts("2026-11-19 12:00"))  # démon arrêté trois jours
        self.assertEqual(self.sched.tick(), {"planned": 1, "dispatched": 1})
        self.assertEqual(self.slots()[1], (2, ts("2026-11-19 16:00"), "pending"))

    def test_drip_feed_over_several_days(self):
        for i in range(1, 6):
            self.approve(i, approved_at=i)
        published = []
        for _ in range(200):
            self.sched.tick()
            for r in self.conn.execute("SELECT clip_id, scheduled_at FROM publish_job WHERE status='queued'"):
                published.append(r["scheduled_at"])
                self.conn.execute("UPDATE publish_job SET status='published' WHERE clip_id=?", (r["clip_id"],))
            nxt = self.sched.next_due()
            if nxt is None:
                break
            self.clock.set(max(self.clock(), nxt))
        self.assertEqual(published, [ts("2026-11-16 09:00"), ts("2026-11-16 13:00"),
                                     ts("2026-11-16 17:00"), ts("2026-11-17 09:00"),
                                     ts("2026-11-17 13:00")])

    def test_ticks_are_idempotent(self):
        self.approve(1)
        self.clock.set(ts("2026-11-16 10:00"))
        first = self.sched.tick()
        self.assertEqual(first, {"planned": 1, "dispatched": 1})
        for _ in range(5):
            self.sched.tick()
        self.assertEqual(self.publish_jobs_in_queue(), ["publish:1"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0], 1)

    def test_all_accounts_of_a_clip_share_the_slot(self):
        self.approve(1, accounts=(1, 2))
        self.clock.set(ts("2026-11-16 10:00"))
        self.sched.tick()
        self.assertEqual(self.publish_jobs_in_queue(), ["publish:1", "publish:2"])

    def test_disabled_schedule_does_nothing(self):
        self.conn.execute("UPDATE schedule SET enabled = 0")
        self.approve(1)
        self.assertEqual(self.sched.tick(), {"planned": 0, "dispatched": 0})

    def test_clips_still_in_review_are_ignored(self):
        self.approve(1)
        self.conn.execute("UPDATE clip SET status = 'review'")
        self.assertEqual(self.sched.tick(), {"planned": 0, "dispatched": 0})

    def test_spacing_survives_restart(self):
        self.approve(1, approved_at=1)
        self.approve(2, approved_at=2)
        self.clock.set(ts("2026-11-16 10:00"))
        self.sched.tick()  # clip 1 à 10h, envoyé
        # Nouveau planificateur (redémarrage du démon) : l'espacement vient de la base.
        sched = Scheduler(self.conn, self.queue, clock=self.clock)
        sched.tick()
        self.assertEqual(self.slots()[1], (2, ts("2026-11-16 14:00"), "pending"))


class TestManualControls(SchedulerTestCase):
    def test_schedule_change_moves_the_reserved_slot(self):
        self.approve(1, approved_at=1)
        self.approve(2, approved_at=2)
        self.clock.set(ts("2026-11-16 09:00"))
        self.sched.tick()  # clip 1 envoyé à 9h, clip 2 réservé à 13h (toutes les 4 h)
        self.assertEqual(self.slots()[1], (2, ts("2026-11-16 13:00"), "pending"))
        services.set_schedule(self.conn, 1, every_hours=1, window="09:00-21:00", timezone="Europe/Paris")
        self.sched.tick()
        self.assertEqual(self.slots()[1], (2, ts("2026-11-16 10:00"), "pending"))  # 9h + 1 h

    def test_schedule_change_before_any_publication(self):
        self.approve(1)
        self.sched.tick()  # réservé à 9h
        services.set_schedule(self.conn, 1, every_hours=4, window="14:00-21:00", timezone="Europe/Paris")
        self.sched.tick()
        self.assertEqual(self.slots()[0], (1, ts("2026-11-16 14:00"), "pending"))

    def test_publish_now_then_spacing_restarts(self):
        self.approve(1, approved_at=1)
        self.approve(2, approved_at=2)
        self.sched.tick()  # clip 1 réservé à 9h
        job2 = self.conn.execute("SELECT id FROM publish_job WHERE clip_id = 2").fetchone()["id"]
        services.publish_now(self.conn, self.queue, job2, now=self.clock())
        self.assertEqual(self.publish_jobs_in_queue(), [f"publish:{job2}"])
        self.sched.tick()
        self.assertEqual(self.slots()[0], (1, ts("2026-11-16 12:00"), "pending"))  # 8h + 4 h
        with self.assertRaises(services.ServiceError):
            services.publish_now(self.conn, self.queue, job2)  # déjà partie

    def test_approve_all_best_scores_first(self):
        self.conn.execute("INSERT INTO bot_account (bot_id, account_id) VALUES (1, 1)")
        for clip_id, score in ((1, 55.0), (2, 90.0), (3, 70.0)):
            self.conn.execute(
                "INSERT INTO clip (id, bot_id, source_id, start_ms, end_ms, status, score, created_at, updated_at) "
                "VALUES (?, 1, 1, ?, ?, 'review', ?, 0, 0)", (clip_id, clip_id * 1000, clip_id * 1000 + 500, score))
        self.assertEqual(services.approve_all(self.conn, 1, now=self.clock()), 3)
        self.sched.tick()
        self.assertEqual(self.slots()[0], (2, ts("2026-11-16 09:00"), "pending"))  # 90 publié en premier
        self.assertEqual([s[0] for s in self.slots()], [2, 3, 1])  # puis 70, puis 55
        self.assertEqual(services.approve_all(self.conn, 1), 0)


if __name__ == "__main__":
    unittest.main()
