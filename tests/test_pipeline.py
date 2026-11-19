import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from autoclip import dryrun
from autoclip.pipeline.analyze import find_clips, split_sentences
from autoclip.pipeline.render import build_ass

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def fake_words(n_sentences=24, words_per_sentence=10, word_s=0.45, gap_s=0.05, pause_s=0.6):
    """Transcription synthétique : phrases régulières, une question de temps en temps."""
    words, t = [], 0.5
    for s in range(n_sentences):
        for w in range(words_per_sentence):
            text = f" mot{s}_{w}"
            if w == words_per_sentence - 1:
                text += "?" if s % 5 == 0 else "."
            words.append({"start": round(t, 3), "end": round(t + word_s, 3), "word": text})
            t += word_s + gap_s
        t += pause_s
    return words


class TestAnalyze(unittest.TestCase):
    def test_sentences_split_on_punctuation_and_silence(self):
        words = [
            {"start": 0.0, "end": 0.3, "word": " Bonjour"},
            {"start": 0.3, "end": 0.6, "word": " toi."},
            {"start": 0.7, "end": 1.0, "word": " Ça"},
            {"start": 3.0, "end": 3.3, "word": " va"},  # silence de 2 s
            {"start": 3.3, "end": 3.6, "word": " ?"},
        ]
        self.assertEqual([s.text for s in split_sentences(words)], ["Bonjour toi.", "Ça", "va ?"])

    def test_clips_respect_boundaries_durations_and_do_not_overlap(self):
        words = fake_words()
        sentences = split_sentences(words)
        starts = {s.start for s in sentences}
        ends = {s.end for s in sentences}
        clips = find_clips(words, min_seconds=20, max_seconds=40, max_clips=4)
        self.assertEqual(len(clips), 4)
        for c in clips:
            self.assertIn(c.start, starts)
            self.assertIn(c.end, ends)
            self.assertTrue(20 <= c.end - c.start <= 40)
        for a, b in zip(clips, clips[1:]):
            self.assertLessEqual(a.end, b.start)

    def test_hook_question_is_preferred(self):
        clips = find_clips(fake_words(), min_seconds=20, max_seconds=30, max_clips=1)
        self.assertTrue(clips[0].sentences[0].text.endswith("?"))

    def test_too_short_transcript_gives_no_clip(self):
        self.assertEqual(find_clips(fake_words(n_sentences=2), min_seconds=20), [])


class TestSubtitles(unittest.TestCase):
    def test_one_event_per_word_with_highlight_and_relative_times(self):
        words = [
            {"start": 10.0, "end": 10.4, "word": " salut"},
            {"start": 10.5, "end": 10.9, "word": " les"},
            {"start": 11.0, "end": 11.4, "word": " amis."},
            {"start": 12.0, "end": 12.4, "word": " {bizarre}"},
        ]
        ass = build_ass(words, 10.0, 13.0, group_size=3)
        events = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
        self.assertEqual(len(events), 4)
        self.assertIn("0:00:00.00,0:00:00.50", events[0])
        self.assertIn("{\\c&H0000FFFF&}SALUT{\\c&H00FFFFFF&} LES AMIS.", events[0])
        self.assertIn("SALUT {\\c&H0000FFFF&}LES{\\c&H00FFFFFF&} AMIS.", events[1])
        self.assertIn("(BIZARRE)", events[3])  # accolades neutralisées
        self.assertIn("PlayResX: 1080", ass)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg requis")
class TestDryRunEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.video = Path(cls.tmp.name) / "source vidéo.mp4"  # espace et accent exprès
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=140",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=140",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(cls.video)],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_video_to_published_clips(self):
        home = Path(self.tmp.name) / "home"
        calls = []

        def transcriber(media, model, language):
            calls.append(media)
            return {"language": "fr", "duration": 140, "words": fake_words()}

        report = dryrun.run(str(self.video), home=home, every_hours=4, max_clips=3,
                            transcriber=transcriber)
        self.assertEqual(report.source_status, "done", report.source_error)
        self.assertEqual(len(report.clips), 3)
        self.assertTrue(report.ok, report.clips)
        self.assertEqual(len(calls), 1)

        # Publications espacées d'au moins 4 h.
        times = sorted(c["published_at"] for c in report.clips)
        for a, b in zip(times, times[1:]):
            self.assertGreaterEqual(b - a, 4 * 3600)

        # Fichiers rendus au format 1080x1920, durée = durée du clip.
        for clip in report.clips:
            probe = json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height:format=duration", "-of", "json", clip["file"]],
                check=True, capture_output=True, text=True).stdout)
            self.assertEqual((probe["streams"][0]["width"], probe["streams"][0]["height"]), (1080, 1920))
            self.assertAlmostEqual(float(probe["format"]["duration"]), clip["end"] - clip["start"], delta=0.3)

        # Trace de chaque « publication » factice.
        self.assertEqual(len(list((home / "cache" / "dryrun").glob("*.json"))), 3)

    def test_stop_at_review_leaves_clips_waiting(self):
        home = Path(self.tmp.name) / "home-review"
        report = dryrun.run(str(self.video), home=home, approve=False, max_clips=2,
                            transcriber=lambda m, mo, l: {"words": fake_words()})
        self.assertEqual([c["status"] for c in report.clips], ["review", "review"])


    def test_auto_approve_schedules_clips_without_review(self):
        from autoclip import db, services
        from autoclip.jobs.queue import JobQueue
        from autoclip.jobs.worker import Worker
        from autoclip.pipeline.cache import Cache
        from autoclip.tasks import TASKS, Context

        home = Path(self.tmp.name) / "home-auto"
        conn = db.connect(home / "autoclip.db")
        queue = JobQueue(conn)
        ctx = Context(conn=conn, queue=queue, cache=Cache(home / "cache"),
                      transcriber=lambda m, mo, l: {"language": "fr", "words": fake_words()})
        bot = services.create_bot(conn, "Auto", dry_run=True)
        services.link_account(conn, bot, services.create_dry_account(conn, "Chaîne"))
        services.update_bot(conn, bot, clip_settings={"max_clips": 2, "auto_approve": True})
        services.add_source(conn, queue, bot, str(self.video))
        Worker(ctx, queue, TASKS).run_until_idle()
        rows = conn.execute("SELECT status, language FROM clip").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("scheduled", "fr"), ("scheduled", "fr")])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM publish_job").fetchone()[0], 2)
        conn.close()


if __name__ == "__main__":
    unittest.main()
