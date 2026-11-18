import tempfile
import threading
import unittest
from pathlib import Path

from autoclip import db
from autoclip.jobs.queue import JobQueue, PermanentError
from autoclip.jobs.worker import Task, Worker

from .helpers import FakeClock


class QueueTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.path)
        self.clock = FakeClock()
        # rng=0.5 -> backoff déterministe : 3/4 du délai nominal
        self.queue = JobQueue(self.conn, clock=self.clock, base_delay=10, max_delay=100,
                              lease_seconds=60, rng=lambda: 0.5)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def status(self, job_id):
        return self.queue.get(job_id)["status"]


class TestEnqueue(QueueTestCase):
    def test_same_key_is_idempotent(self):
        a = self.queue.enqueue("render", {"clip_id": 1}, key="render:1")
        b = self.queue.enqueue("render", {"clip_id": 1}, key="render:1")
        self.assertEqual(a, b)
        self.assertEqual(self.queue.stats(), {"queued": 1})

    def test_default_key_derives_from_payload(self):
        a = self.queue.enqueue("render", {"clip_id": 1, "x": 2})
        b = self.queue.enqueue("render", {"x": 2, "clip_id": 1})
        c = self.queue.enqueue("render", {"clip_id": 2})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_enqueue_of_done_job_does_not_rerun_it(self):
        jid = self.queue.enqueue("ingest", {}, key="k")
        self.queue.complete(self.queue.claim(), "ok")
        self.assertEqual(self.queue.enqueue("ingest", {}, key="k"), jid)
        self.assertIsNone(self.queue.claim())


class TestClaim(QueueTestCase):
    def test_fifo_and_run_after(self):
        later = self.queue.enqueue("a", {}, key="later", run_after=self.clock() + 50)
        first = self.queue.enqueue("a", {}, key="first")
        second = self.queue.enqueue("a", {}, key="second")
        self.assertEqual(self.queue.claim().id, first)
        self.assertEqual(self.queue.claim().id, second)
        self.assertIsNone(self.queue.claim())
        self.clock.advance(50)
        self.assertEqual(self.queue.claim().id, later)

    def test_filter_by_kind(self):
        self.queue.enqueue("publish", {}, key="p")
        r = self.queue.enqueue("render", {}, key="r")
        self.assertEqual(self.queue.claim(["render"]).id, r)
        self.assertIsNone(self.queue.claim(["render"]))

    def test_claim_increments_attempts_and_leases(self):
        jid = self.queue.enqueue("a", {"v": 1})
        job = self.queue.claim()
        self.assertEqual((job.id, job.attempts, job.payload), (jid, 1, {"v": 1}))
        row = self.queue.get(jid)
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["lease_until"], self.clock() + 60)

    def test_lease_by_kind(self):
        self.queue.lease_by_kind = {"transcribe": 3600}
        jid = self.queue.enqueue("transcribe", {})
        self.queue.claim()
        self.assertEqual(self.queue.get(jid)["lease_until"], self.clock() + 3600)


class TestRetries(QueueTestCase):
    def test_backoff_is_exponential_capped_and_jittered(self):
        q = JobQueue(self.conn, base_delay=10, max_delay=100, rng=lambda: 0.0)
        self.assertEqual([q.backoff(n) for n in (1, 2, 3, 4, 5, 9)], [5, 10, 20, 40, 50, 50])
        q.rng = lambda: 1.0
        self.assertEqual([q.backoff(n) for n in (1, 2, 5)], [10, 20, 100])

    def test_failure_requeues_with_backoff(self):
        jid = self.queue.enqueue("a", {}, max_attempts=3)
        self.assertEqual(self.queue.fail(self.queue.claim(), "boom"), "queued")
        row = self.queue.get(jid)
        self.assertEqual(row["run_after"], self.clock() + 7.5)
        self.assertEqual(row["last_error"], "boom")
        self.assertIsNone(self.queue.claim())  # pas avant la fin du délai
        self.clock.advance(7.5)
        job = self.queue.claim()
        self.assertEqual(job.attempts, 2)
        self.queue.fail(job, "boom")
        self.assertEqual(self.queue.get(jid)["run_after"], self.clock() + 15)

    def test_gives_up_after_max_attempts(self):
        jid = self.queue.enqueue("a", {}, max_attempts=2)
        self.queue.fail(self.queue.claim(), "1")
        self.clock.advance(1000)
        self.assertEqual(self.queue.fail(self.queue.claim(), "2"), "failed")
        self.assertEqual(self.status(jid), "failed")
        self.clock.advance(1000)
        self.assertIsNone(self.queue.claim())

    def test_permanent_error_fails_immediately(self):
        jid = self.queue.enqueue("a", {}, max_attempts=5)
        self.assertEqual(self.queue.fail(self.queue.claim(), "nope", permanent=True), "failed")
        self.assertEqual(self.status(jid), "failed")

    def test_manual_retry_of_failed_job(self):
        jid = self.queue.enqueue("a", {}, max_attempts=1)
        self.queue.fail(self.queue.claim(), "x")
        self.assertTrue(self.queue.retry(jid))
        self.assertEqual(self.queue.claim().attempts, 1)


class TestCrashRecovery(QueueTestCase):
    def test_expired_lease_is_reclaimed(self):
        jid = self.queue.enqueue("a", {})
        crashed = self.queue.claim()
        self.assertIsNone(self.queue.claim())  # bail encore valide
        self.clock.advance(61)
        retaken = self.queue.claim()
        self.assertEqual((retaken.id, retaken.attempts), (jid, 2))
        # Le worker « mort » qui se réveille ne peut plus écraser l'état.
        self.assertFalse(self.queue.complete(crashed, "late"))
        self.assertEqual(self.queue.fail(crashed, "late"), "stale")
        self.assertTrue(self.queue.complete(retaken, "ok"))
        self.assertEqual(self.status(jid), "done")

    def test_expired_lease_on_last_attempt_fails(self):
        jid = self.queue.enqueue("a", {}, max_attempts=1)
        self.queue.claim()
        self.clock.advance(61)
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.status(jid), "failed")

    def test_heartbeat_extends_lease(self):
        self.queue.enqueue("a", {})
        job = self.queue.claim()
        self.clock.advance(50)
        self.assertTrue(self.queue.heartbeat(job))
        self.clock.advance(50)
        self.assertIsNone(self.queue.claim())

    def test_requeue_orphans_after_restart(self):
        jid = self.queue.enqueue("a", {})
        self.queue.claim()
        self.conn.close()
        # « Redémarrage » : nouvelle connexion sur le même fichier.
        self.conn = db.connect(self.path)
        queue = JobQueue(self.conn, clock=self.clock)
        self.assertEqual(queue.requeue_orphans(), 1)
        job = queue.claim()
        self.assertEqual((job.id, job.attempts), (jid, 2))

    def test_concurrent_workers_never_claim_the_same_job(self):
        ids = {self.queue.enqueue("a", {"n": n}) for n in range(200)}
        claimed: list[int] = []
        lock = threading.Lock()

        def run():
            conn = db.connect(self.path)
            queue = JobQueue(conn, clock=self.clock)
            while (job := queue.claim()) is not None:
                with lock:
                    claimed.append(job.id)
                queue.complete(job)
            conn.close()

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(claimed), 200)
        self.assertEqual(set(claimed), ids)


class TestWorker(QueueTestCase):
    def test_worker_runs_retries_and_reports_dead_jobs(self):
        calls = {"flaky": 0}
        dead = []

        def ok(ctx, payload):
            return payload["v"] * 2

        def flaky(ctx, payload):
            calls["flaky"] += 1
            if calls["flaky"] < 3:
                raise RuntimeError("temporaire")
            return "enfin"

        def broken(ctx, payload):
            raise PermanentError("cassé")

        worker = Worker(None, self.queue, {
            "ok": Task(ok),
            "flaky": Task(flaky),
            "broken": Task(broken, on_dead=lambda ctx, p, err: dead.append((p, err))),
        })
        a = self.queue.enqueue("ok", {"v": 21})
        b = self.queue.enqueue("flaky", {}, max_attempts=5)
        c = self.queue.enqueue("broken", {"id": 7})

        for _ in range(5):
            worker.run_until_idle()
            self.clock.advance(1000)

        self.assertEqual(self.queue.get(a)["result"], "42")
        self.assertEqual(self.status(b), "done")
        self.assertEqual(calls["flaky"], 3)
        self.assertEqual(self.status(c), "failed")
        self.assertEqual(dead, [({"id": 7}, "cassé")])

    def test_unknown_kinds_are_left_alone(self):
        jid = self.queue.enqueue("other", {})
        worker = Worker(None, self.queue, {"ok": Task(lambda c, p: None)})
        self.assertEqual(worker.run_until_idle(), 0)
        self.assertEqual(self.status(jid), "queued")


if __name__ == "__main__":
    unittest.main()
