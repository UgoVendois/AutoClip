import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from autoclip import db, secrets, services
from autoclip.ai.base import ScoringRequest, Segment
from autoclip.ai.budget import score_with_budget, used_tokens
from autoclip.ai.providers import FakeProvider
from autoclip.discovery import DiscoveredItem, use_discoverer
from autoclip.discovery.twitch import TwitchDiscoverer, parse_duration, store_chat_event
from autoclip.jobs.queue import JobQueue
from autoclip.jobs.worker import Worker
from autoclip.pipeline.cache import Cache
from autoclip.scheduler import Scheduler
from autoclip.signals.peaks import comment_timestamps, density, find_peaks, peak_windows
from autoclip.tasks import TASKS, Context

from .helpers import FakeClock

HAS_FFMPEG = shutil.which("ffmpeg") is not None
T0 = datetime(2026, 11, 16, 20, 0).timestamp()  # début du live de test
BURSTS = (60, 150, 240)  # secondes où le chat s'emballe


def chat_events(duration=300, bursts=BURSTS, per_burst=45):
    """Chat de fond régulier (1 message / 4 s) + rafales de messages aux pics."""
    events = [t + 0.5 for t in range(0, duration, 4)]
    for b in bursts:
        events += [b + i * (12 / per_burst) for i in range(per_burst)]
    return sorted(events)


# -- détection de pics -----------------------------------------------------------

class TestPeaks(unittest.TestCase):
    def test_density_counts_sliding_window(self):
        series = dict(density([1, 2, 3, 31, 32], 60, window=30, step=10))
        self.assertEqual(series, {0: 3, 10: 2, 20: 2, 30: 2})

    def test_uniform_chat_has_no_peak(self):
        events = [float(t) for t in range(0, 600, 3)]
        self.assertEqual(peak_windows(events, 600, top_n=5), [])

    def test_bursts_are_found_and_ranked(self):
        events = chat_events(per_burst=45)
        events += [100 + i * 0.2 for i in range(20)]  # petit pic plus faible
        windows = peak_windows(events, 300, window=30, step=5, top_n=3, margin=8, lead=20)
        self.assertEqual(len(windows), 3)
        for w in windows:  # chaque fenêtre contient une des grosses rafales
            self.assertTrue(any(w.start <= b and b + 12 <= w.end for b in BURSTS), w)
        self.assertTrue(all(w.duration <= 20 + 15 + 2 * 8 + 1e-6 for w in windows))
        for w in windows:  # centrée sur la rafale (12 s de messages)
            self.assertTrue(any(b <= w.peak <= b + 12 for b in BURSTS), w)

    def test_top_n_and_separation(self):
        peaks = find_peaks([(t, 10 if t in (50, 55, 200) else 1) for t in range(0, 300, 5)],
                           top_n=5, min_separation=30)
        self.assertEqual([t for t, _ in sorted(peaks)], [50, 200])  # 55 trop proche de 50

    def test_overlapping_windows_are_merged(self):
        events = chat_events(bursts=(100, 125), per_burst=40)
        windows = peak_windows(events, 300, window=30, top_n=4, margin=8, lead=20)
        self.assertEqual(len(windows), 1)
        self.assertLessEqual(windows[0].start, 100)
        self.assertGreaterEqual(windows[0].end, 137)

    def test_comment_timestamps(self):
        comments = ["le moment à 12:34 😂", "1:02:03 incroyable", "rien", "score 3-2", "99:99", "10:05 et 10:06"]
        self.assertEqual(comment_timestamps(comments), [754, 3723, 605, 606])
        self.assertEqual(comment_timestamps(["50:00"], duration=600), [])


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg requis")
class TestAudioEnergy(unittest.TestCase):
    def test_loud_moment_is_found(self):
        """Mesure réelle par ffmpeg : 40 s calmes, 10 s fortes, 40 s calmes."""
        from autoclip.signals.audio import energy_windows, loudness_series

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.m4a"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=90",
                            "-af", "volume='if(between(t,40,50),1,0.03)':eval=frame", str(audio)], check=True)
            series = loudness_series(audio)
            self.assertGreater(len(series), 80)
            windows = energy_windows(series, 90, window=10, top_n=3, margin=2)
        self.assertTrue(windows)
        self.assertTrue(35 <= windows[0].peak <= 55, windows[0])


class TestTwitchHelpers(unittest.TestCase):
    def test_parse_duration(self):
        self.assertEqual(parse_duration("3h2m10s"), 10930)
        self.assertEqual(parse_duration("45s"), 45)

    def test_chat_event_storage_keeps_no_username(self):
        conn = db.connect(":memory:")
        msg = {"metadata": {"message_type": "notification", "message_timestamp": "2026-11-16T19:00:05.5Z"},
               "payload": {"subscription": {"type": "channel.chat.message"},
                           "event": {"broadcaster_user_id": "42", "chatter_user_login": "quelquun",
                                     "message": {"text": "KEKW"}}}}
        self.assertFalse(store_chat_event(conn, msg, {}))  # hors live : ignoré
        self.assertTrue(store_chat_event(conn, msg, {"42": "s1"}))
        row = dict(conn.execute("SELECT * FROM chat_message").fetchone())
        self.assertEqual((row["channel_id"], row["stream_id"], row["text"]), ("42", "s1", "KEKW"))
        self.assertNotIn("quelquun", json.dumps(row))


# -- banc de test : une fausse chaîne Twitch sur une vraie vidéo locale ------------

class LocalTwitch(TwitchDiscoverer):
    """Twitch sans réseau : la VOD est un fichier local, le chat est en base."""

    def __init__(self, video: Path, items: list[DiscoveredItem]):
        super().__init__()
        self.video = video
        self.items = items
        self.sections: list[tuple[float, float]] = []
        self.audio_downloads = 0

    def resolve_channel(self, channel):
        return "1234"

    def list_recent(self, channel_id, since):
        return [i for i in self.items if i.published_at > since]

    def download_section(self, item, start, end, dest):
        self.sections.append((start, end))
        out = dest.with_suffix(".mp4")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start), "-i", str(self.video),
                        "-t", str(end - start), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(out)],
                       check=True)
        return out

    def download_audio(self, item, dest, should_stop=None):
        self.audio_downloads += 1
        raise AssertionError("pas de téléchargement audio quand le chat est disponible")


def section_transcriber(calls):
    """Faux Whisper : des phrases régulières sur toute la durée de l'extrait."""
    def transcribe(media, model, language):
        duration = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                         "-of", "default=nw=1:nk=1", str(media)],
                                        capture_output=True, text=True).stdout)
        calls.append(duration)
        words, t, n = [], 0.3, 0
        while t + 0.45 < duration:
            n += 1
            text = f" mot{n}" + ("!" if n % 9 == 0 else "")
            words.append({"start": round(t, 2), "end": round(t + 0.4, 2), "word": text})
            t += 0.5
        return {"language": "fr", "words": words}
    return transcribe


class DiscoveryHarness(unittest.TestCase):
    video: Path

    @classmethod
    def setUpClass(cls):
        if not HAS_FFMPEG:
            raise unittest.SkipTest("ffmpeg requis")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.video = Path(cls.tmp.name) / "vod.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=640x360:rate=15:duration=300", "-f", "lavfi", "-i",
                        "sine=frequency=500:duration=300", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", "-shortest", str(cls.video)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        secrets.use_backend(secrets.MemoryBackend())
        self.home = Path(tempfile.mkdtemp(dir=self.tmp.name))
        self.conn = db.connect(self.home / "autoclip.db")
        self.clock = FakeClock(T0 + 3 * 3600)
        self.queue = JobQueue(self.conn, clock=self.clock)
        self.provider = FakeProvider()
        self.transcribed: list[float] = []
        self.ctx = Context(conn=self.conn, queue=self.queue, cache=Cache(self.home / "cache"),
                           clock=self.clock, transcriber=section_transcriber(self.transcribed),
                           ai_override=self.provider)
        self.worker = Worker(self.ctx, self.queue, TASKS)
        self.scheduler = Scheduler(self.conn, self.queue, clock=self.clock)
        self.vod = DiscoveredItem(external_id="v1", url="https://www.twitch.tv/videos/v1", title="Gros live",
                                  published_at=T0, duration=300, channel_id="1234", stream_id="s1")
        self.twitch = LocalTwitch(self.video, [self.vod])
        use_discoverer("twitch", self.twitch)
        self.bot_id = services.create_bot(self.conn, "Clips Twitch", dry_run=True)
        services.update_bot(self.conn, self.bot_id, ai_settings={"provider": "fake", "max_windows": 3,
                                                                 "min_score": 50, "clips_per_video": 3})

    def tearDown(self):
        use_discoverer("twitch", None)
        secrets.use_backend(None)
        self.conn.close()

    def record_chat(self, events):
        self.conn.executemany(
            "INSERT INTO chat_message (platform, channel_id, stream_id, ts, text) VALUES ('twitch', '1234', 's1', ?, 'msg')",
            [(T0 + e,) for e in events])

    def run_all(self):
        for _ in range(50):
            self.scheduler.tick()
            if not self.worker.run_until_idle():
                break


class TestDeduplication(DiscoveryHarness):
    def test_same_vod_is_queued_once(self):
        self.record_chat(chat_events())
        watch_id = services.add_watch(self.conn, self.bot_id, "twitch", "streamer", 30, now=self.clock())
        services.add_watch(self.conn, self.bot_id, "twitch", "streamer", 30, now=self.clock())  # doublon de surveillance
        self.run_all()
        self.clock.advance(3600)  # nouveau passage : la même VOD revient
        self.run_all()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM watch").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM discovered").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM job WHERE kind = 'presort'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM job WHERE kind = 'discover'").fetchone()[0], 2)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertIsNotNone(self.conn.execute("SELECT last_checked_at FROM watch WHERE id = ?",
                                               (watch_id,)).fetchone()[0])

    def test_live_vod_waits_until_stream_ends(self):
        self.twitch.items = [DiscoveredItem(**{**self.vod.__dict__, "live": True})]
        services.add_watch(self.conn, self.bot_id, "twitch", "streamer", 30, now=self.clock())
        self.run_all()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM discovered").fetchone()[0], 0)
        self.twitch.items = [self.vod]
        self.record_chat(chat_events())
        self.clock.advance(1800)
        self.run_all()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM discovered").fetchone()[0], 1)

    def test_old_content_before_backfill_is_ignored(self):
        services.update_bot(self.conn, self.bot_id, ai_settings={"backfill_hours": 1})
        self.twitch.items = [DiscoveredItem(**{**self.vod.__dict__, "published_at": T0 - 5 * 86400})]
        services.add_watch(self.conn, self.bot_id, "twitch", "streamer", 30, now=self.clock())
        self.run_all()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM discovered").fetchone()[0], 0)


class TestTwitchVodEndToEnd(DiscoveryHarness):
    """Critère de réussite : seules les fenêtres autour des pics vont à l'IA, et
    les clips candidats arrivent dans la file avec leur score et leur raison."""

    def test_only_peak_windows_reach_the_ai(self):
        self.record_chat(chat_events())
        services.add_watch(self.conn, self.bot_id, "twitch", "streamer", 60, now=self.clock())
        self.run_all()

        # Téléchargement et transcription : seulement les fenêtres, jamais la VOD entière.
        self.assertEqual(len(self.twitch.sections), 3)
        self.assertEqual(len(self.transcribed), 3)
        self.assertTrue(all(d < 60 for d in self.transcribed), self.transcribed)
        self.assertLess(sum(self.transcribed), 300 * 0.6)
        for start, end in self.twitch.sections:
            self.assertTrue(any(start <= b < end for b in BURSTS), (start, end))

        # Un seul appel à l'IA, avec uniquement des segments situés autour des pics.
        self.assertEqual(len(self.provider.calls), 1)
        segments = self.provider.calls[0].segments
        self.assertGreater(len(segments), 0)
        for seg in segments:  # chaque segment est dans une fenêtre qui contient une rafale
            self.assertTrue(any(s0 <= seg.start and seg.end <= s1 for s0, s1 in self.twitch.sections), seg)
            self.assertTrue(any(b - 40 <= seg.start and seg.end <= b + 40 for b in BURSTS), seg)
            self.assertEqual(seg.signal, "chat")
        # Un extrait par fenêtre démarre juste avant le moment fort (pic du chat moins son retard).
        hooks = [seg for seg in segments if seg.id.endswith("h")]
        self.assertEqual(len(hooks), 3)
        for seg in hooks:
            self.assertTrue(any(b - 15 <= seg.start <= b for b in BURSTS), seg)  # démarre avant la rafale
        # L'IA reçoit aussi des images clés.
        self.assertEqual(self.provider.images_seen, 2 * len(segments))

        # Les clips attendent la validation, avec score et raison.
        clips = services.list_clips(self.conn, bot_id=self.bot_id)
        self.assertGreater(len(clips), 0)
        for c in clips:
            self.assertEqual(c["status"], "review", c["error"])
            self.assertEqual(c["origin"], "ai")
            self.assertGreaterEqual(c["score"], 50)
            self.assertIn("pic de chat", c["reason"])
            self.assertTrue(c["hook"])
            self.assertTrue(Path(c["file_path"]).is_file())
        self.assertEqual(self.conn.execute("SELECT status FROM discovered").fetchone()[0], "done")

        # Rendu correct depuis l'extrait (décalage) : durée du fichier = durée du clip.
        c = clips[0]
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                              "default=nw=1:nk=1", c["file_path"]], capture_output=True, text=True).stdout
        self.assertAlmostEqual(float(out), (c["end_ms"] - c["start_ms"]) / 1000, delta=0.3)

    def test_without_chat_falls_back_to_audio(self):
        services.add_watch(self.conn, self.bot_id, "twitch", "streamer", 60, now=self.clock())
        with self.assertLogs("autoclip.jobs.worker", level="WARNING"):
            self.run_all()
        self.assertEqual(self.twitch.audio_downloads, 1)  # pas de chat -> énergie audio
        self.assertEqual(self.provider.calls, [])


# -- budget de tokens ---------------------------------------------------------------

class TestTokenBudget(DiscoveryHarness):
    def request(self, n=3, text="un deux trois"):
        return ScoringRequest("vidéo", "chaîne", [Segment(f"s{i}", i * 30, i * 30 + 25, f"{text} {i}", "chat", 2.0)
                                                  for i in range(n)])

    def test_call_refused_when_estimate_exceeds_budget(self):
        estimate = self.provider.estimate_tokens(self.request())
        outcome = score_with_budget(self.conn, self.provider, self.request(), bot_id=self.bot_id,
                                    daily_budget=estimate - 1, now=self.clock())
        self.assertIsNone(outcome.scores)
        self.assertEqual(self.provider.calls, [])  # rien n'a été envoyé
        alerts = services.list_alerts(self.conn)
        self.assertEqual([a["kind"] for a in alerts], ["token_budget"])

    def test_usage_is_recorded_and_cache_costs_nothing(self):
        first = score_with_budget(self.conn, self.provider, self.request(), bot_id=self.bot_id,
                                  daily_budget=100_000, now=self.clock())
        self.assertGreater(first.tokens, 0)
        day = datetime.fromtimestamp(self.clock()).date().isoformat()
        self.assertEqual(used_tokens(self.conn, self.bot_id, day), first.tokens)
        again = score_with_budget(self.conn, self.provider, self.request(), bot_id=self.bot_id,
                                  daily_budget=100_000, now=self.clock())
        self.assertTrue(again.cached)
        self.assertEqual(again.tokens, 0)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(again.scores.keys(), first.scores.keys())

    def test_budget_spent_over_several_calls(self):
        budget = self.provider.estimate_tokens(self.request()) * 2
        results = [score_with_budget(self.conn, self.provider, self.request(text=f"vidéo {k}"),
                                     bot_id=self.bot_id, daily_budget=budget, now=self.clock())
                   for k in range(5)]
        sent = [r for r in results if r.scores is not None]
        self.assertGreaterEqual(len(sent), 1)
        self.assertLess(len(sent), 5)
        day = datetime.fromtimestamp(self.clock()).date().isoformat()
        self.assertLessEqual(used_tokens(self.conn, self.bot_id, day), budget)
        self.assertEqual(len(services.list_alerts(self.conn)), 1)  # une alerte par jour, pas une par appel

    def test_pipeline_defers_cleanly_then_resumes_next_day(self):
        services.update_bot(self.conn, self.bot_id, ai_settings={"daily_token_budget": 10})
        self.record_chat(chat_events())
        services.add_watch(self.conn, self.bot_id, "twitch", "streamer", 60 * 24, now=self.clock())
        self.run_all()
        self.assertEqual(self.conn.execute("SELECT status FROM discovered").fetchone()[0], "deferred")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(services.list_clips(self.conn, bot_id=self.bot_id), [])
        self.assertEqual(self.queue.stats().get("failed", 0), 0)  # arrêt propre, pas d'échec
        self.assertEqual([a["kind"] for a in services.list_alerts(self.conn)], ["token_budget"])

        services.update_bot(self.conn, self.bot_id, ai_settings={"daily_token_budget": 100_000})
        self.clock.advance(86400)  # le lendemain
        self.run_all()
        self.assertEqual(self.conn.execute("SELECT status FROM discovered").fetchone()[0], "done")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertGreater(len(services.list_clips(self.conn, bot_id=self.bot_id, status="review")), 0)
        self.assertEqual(len(self.transcribed), 3)  # les transcriptions n'ont pas été refaites


class TestMigration(unittest.TestCase):
    def test_v1_database_is_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            import sqlite3

            old = sqlite3.connect(path)
            old.executescript(db.SCHEMA)
            old.execute("PRAGMA user_version=1")
            old.execute("INSERT INTO bot (name, created_at) VALUES ('ancien', 0)")
            old.commit()
            old.close()
            conn = db.connect(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            bot = conn.execute("SELECT * FROM bot").fetchone()
            self.assertEqual((bot["name"], bot["ai_settings"]), ("ancien", "{}"))
            conn.close()


if __name__ == "__main__":
    unittest.main()
