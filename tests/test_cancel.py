"""Annulation d'une analyse en cours et temps restant estimé."""

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from autoclip import db, eta, identity, secrets, services
from autoclip.ai.images import FakeImage
from autoclip.ai.providers import FakeProvider
from autoclip.jobs.queue import Cancelled, JobQueue
from autoclip.jobs.worker import Worker
from autoclip.pipeline.cache import Cache
from autoclip.signals.audio import loudness_series
from autoclip.tasks import TASKS, Context

from .helpers import FakeClock
from .test_discovery import T0, DiscoveryHarness

HAS_FFMPEG = shutil.which("ffmpeg") is not None


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg requis")
class TestLoudnessStop(unittest.TestCase):
    def test_ffmpeg_is_stopped_on_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "long.m4a"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=600",
                            str(audio)], check=True)
            started = time.monotonic()
            with self.assertRaises(Cancelled):
                loudness_series(audio, should_stop=lambda: True)
            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue(loudness_series(audio, should_stop=lambda: False))  # sans annulation : mesures


class AudioVod(DiscoveryHarness):
    """VOD sans chat : passe par le téléchargement de l'audio et la mesure du son."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Son calme avec trois moments forts (à 60, 150 et 240 s).
        cls.audio = Path(cls.tmp.name) / "audio.m4a"
        loud = "+".join(f"between(t,{b},{b + 8})" for b in (60, 150, 240))
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=300",
                        "-af", f"volume='if({loud},1,0.03)':eval=frame", str(cls.audio)], check=True)

    def setUp(self):
        super().setUp()
        self.on_download = None
        harness = self

        def download_audio(item, dest, should_stop=None):
            harness.twitch.audio_downloads += 1
            if harness.on_download:
                harness.on_download()
            if should_stop and should_stop():
                raise Cancelled("téléchargement annulé")
            return harness.audio
        self.twitch.download_audio = download_audio  # aucun message de chat en base : signal audio

    def discovered(self):
        return self.conn.execute("SELECT * FROM discovered").fetchone()


class TestCancelDiscovered(AudioVod):
    def test_cancel_while_downloading_stops_everything(self):
        services.add_watch(self.conn, self.bot_id, "twitch", "streamer", 60, now=self.clock())
        self.on_download = lambda: services.cancel_discovered(self.conn, self.discovered()["id"])
        self.run_all()
        row = self.discovered()
        self.assertEqual((row["status"], row["error"]), ("skipped", services.CANCELLED))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM candidate_window").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM clip").fetchone()[0], 0)
        failed = self.conn.execute("SELECT COUNT(*) FROM job WHERE status = 'failed'").fetchone()[0]
        self.assertEqual(failed, 0)  # une annulation n'est pas une erreur

    def test_cancel_after_presort_skips_transcription_and_ai(self):
        services.add_watch(self.conn, self.bot_id, "twitch", "streamer", 60, now=self.clock())
        self.scheduler.tick()
        while self.discovered() is None or self.discovered()["status"] == "new":
            self.assertTrue(self.worker.run_once())
        services.cancel_discovered(self.conn, self.discovered()["id"])
        src = self.conn.execute("SELECT status, error FROM source").fetchone()
        self.assertEqual((src["status"], src["error"]), ("failed", services.CANCELLED))
        self.run_all()
        self.assertEqual(self.transcribed, [])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM clip").fetchone()[0], 0)
        with self.assertRaises(services.ServiceError):  # déjà arrêtée
            services.cancel_discovered(self.conn, self.discovered()["id"])

    def test_cancel_through_its_source(self):
        services.add_watch(self.conn, self.bot_id, "twitch", "streamer", 60, now=self.clock())
        self.scheduler.tick()
        while self.discovered() is None or self.discovered()["status"] == "new":
            self.worker.run_once()
        services.cancel_source(self.conn, self.discovered()["source_id"])
        self.assertEqual(self.discovered()["error"], services.CANCELLED)


class TestEta(AudioVod):
    def test_estimate_follows_the_pipeline(self):
        services.add_watch(self.conn, self.bot_id, "twitch", "streamer", 60, now=self.clock())
        self.scheduler.tick()
        self.worker.run_once()  # discover : la VOD est trouvée, son analyse est en file
        d = self.discovered()
        first = eta.estimate(self.conn, self.clock())[("d", d["id"])]
        ai = services.ai_settings(services.get_bot(self.conn, self.bot_id))
        # analyse du son + fenêtres + notation + rendus
        expected = (300 * eta.PRESORT_PER_SECOND + ai["max_windows"] * eta.DEFAULT_SECONDS["prepare_window"]
                    + eta.DEFAULT_SECONDS["ai_score"] + ai["clips_per_video"] * eta.DEFAULT_SECONDS["render"])
        self.assertAlmostEqual(first, expected, delta=1)

        self.worker.run_once()  # presort fait : il reste les fenêtres réelles
        windows = self.conn.execute("SELECT COUNT(*) FROM candidate_window").fetchone()[0]
        after = eta.estimate(self.conn, self.clock())[("d", d["id"])]
        self.assertLess(after, first)
        self.assertAlmostEqual(after, windows * eta.DEFAULT_SECONDS["prepare_window"]
                               + eta.DEFAULT_SECONDS["ai_score"]
                               + ai["clips_per_video"] * eta.DEFAULT_SECONDS["render"], delta=1)

    def test_measured_durations_replace_defaults(self):
        q = self.queue
        for i in range(3):
            q.enqueue("render", {"clip_id": 1000 + i})
            job = q.claim()
            self.clock.advance(12)
            q.complete(job, {"ok": True})
        durations, _ = eta._measured(self.conn)
        self.assertEqual(durations["render"], 12)


class TestCancelImages(unittest.TestCase):
    def setUp(self):
        secrets.use_backend(secrets.MemoryBackend())
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.conn = db.connect(home / "autoclip.db")
        self.clock = FakeClock()
        self.queue = JobQueue(self.conn, clock=self.clock)
        self.ctx = Context(conn=self.conn, queue=self.queue, cache=Cache(home / "cache"), clock=self.clock,
                           ai_override=FakeProvider(), image_override=FakeImage())
        self.worker = Worker(self.ctx, self.queue, TASKS)
        self.bot = services.create_bot(self.conn, "Bot", dry_run=True)

    def tearDown(self):
        secrets.use_backend(None)
        self.conn.close()
        self.tmp.cleanup()

    def test_cancelled_images_are_not_generated(self):
        ident_id = identity.start(self.conn, self.queue, self.bot, "humour")
        self.worker.run_once()  # proposition (texte) faite, visuels en file
        row = identity.latest(self.conn, self.bot)
        self.assertEqual(row["images_status"], "pending")
        self.assertGreater(eta.estimate(self.conn, self.clock())[("i", ident_id)], 0)
        identity.cancel_images(self.conn, ident_id)
        self.worker.run_until_idle()
        row = identity.latest(self.conn, self.bot)
        self.assertEqual((row["images_status"], row["error"]), ("failed", services.CANCELLED))
        self.assertIsNone(row["avatar_path"])
        with self.assertRaises(services.ServiceError):
            identity.cancel_images(self.conn, ident_id)
        identity.regenerate_images(self.conn, self.queue, ident_id)  # relance possible
        self.worker.run_until_idle()
        self.assertEqual(identity.latest(self.conn, self.bot)["images_status"], "ready")


if __name__ == "__main__":
    unittest.main()
