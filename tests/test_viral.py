"""Phase « clips viraux » : mise en page, coupe sur l'action, silences, accroche, images pour l'IA."""

import base64
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autoclip.ai import remote
from autoclip.ai.base import ScoringRequest, Segment
from autoclip.ai.budget import cache_key
from autoclip.ai.providers import FakeProvider
from autoclip.ai.remote import GeminiProvider, MistralProvider, OpenAIProvider
from autoclip.pipeline import layout as lay
from autoclip.pipeline.analyze import hook_clip
from autoclip.pipeline.render import build_ass, keep_intervals, remap_words, render_clip

from .test_identity import SCORES, FakeHTTP

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def words_at(times, text="mot"):
    return [{"start": t, "end": t + 0.4, "word": f" {text}{i}" + ("." if i % 5 == 4 else "")}
            for i, t in enumerate(times)]


class TestHookCut(unittest.TestCase):
    def test_starts_just_before_the_action(self):
        words = words_at([10 + i * 0.5 for i in range(80)])
        c = hook_clip(words, anchor=30, window=(5, 60), min_seconds=20, max_seconds=40)
        self.assertLessEqual(30 - c.start, 4.5)  # action dans les premières secondes
        self.assertGreaterEqual(c.end - c.start, 20)
        self.assertTrue(c.text.endswith("."), c.text)  # fin sur une fin de phrase

    def test_works_without_any_speech(self):
        c = hook_clip([], anchor=30, window=(5, 60), min_seconds=20, max_seconds=40)
        self.assertEqual((c.start, c.end), (26, 46))
        self.assertEqual(c.title, "")

    def test_never_starts_mid_word_and_stays_in_window(self):
        words = [{"start": 25.8, "end": 26.5, "word": " long"}]
        c = hook_clip(words, anchor=30, window=(5, 60), min_seconds=20, max_seconds=40)
        self.assertEqual(c.start, 25.8)
        c = hook_clip([], anchor=58, window=(5, 60), min_seconds=20, max_seconds=40)
        self.assertLessEqual(c.end, 60)
        self.assertIsNone(hook_clip([], anchor=10, window=(0, 15), min_seconds=20))


class TestSilences(unittest.TestCase):
    def test_keep_intervals_skip_silence_but_not_words(self):
        keep = keep_intervals(20, [(5, 9), (15, 15.3)], [{"start": 7, "end": 7.3, "word": " chut"}])
        # 5-9 coupé de part et d'autre du mot chuchoté ; 15-15.3 trop court pour être coupé.
        self.assertEqual(keep, [(0.0, 5.15), (6.85, 7.45), (8.85, 20.0)])

    def test_words_are_remapped(self):
        keep = [(0.0, 5.0), (8.0, 10.0)]
        out = remap_words([{"start": 1, "end": 1.5, "word": "a"}, {"start": 6, "end": 6.5, "word": "coupé"},
                           {"start": 8.5, "end": 9, "word": "b"}], keep)
        self.assertEqual([(w["word"], w["start"]) for w in out], [("a", 1), ("b", 5.5)])


class TestFacecamDetection(unittest.TestCase):
    def test_stable_face_is_a_facecam_moving_faces_are_not(self):
        cam = lay.Box(500, 300, 40, 40)
        frames = [[cam, lay.Box(100 + i * 50, 50, 60, 60)] for i in range(8)]  # PNJ qui bouge
        self.assertEqual(lay.stable_face(frames, 640), cam)
        self.assertIsNone(lay.stable_face([[lay.Box(100 + i * 50, 50, 60, 60)] for i in range(8)], 640))

    def test_full_frame_face_is_not_a_facecam(self):
        self.assertIsNone(lay.stable_face([[lay.Box(200, 50, 300, 300)]] * 8, 640))

    def test_facecam_region_is_16_9_inside_frame(self):
        r = lay.facecam_region(lay.Box(1800, 950, 80, 80), 1920, 1080)
        self.assertAlmostEqual(r.w / r.h, 16 / 9, delta=0.02)
        self.assertLessEqual(r.x + r.w, 1920)
        self.assertLessEqual(r.y + r.h, 1080)

    def test_auto_without_face_falls_back_to_fit_and_is_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "l.json"
            calls = []
            with mock.patch.object(lay, "detect_facecam", side_effect=lambda *a, **k: calls.append(1)):
                self.assertEqual(lay.choose("auto", Path("x.mp4"), 0, 10, cache), ("fit", None))
                self.assertEqual(lay.choose("auto", Path("x.mp4"), 0, 10, cache), ("fit", None))
            self.assertEqual(len(calls), 1)
            self.assertEqual(lay.choose("crop", Path("x.mp4"), 0, 10, cache), ("crop", None))


class TestHookText(unittest.TestCase):
    def test_hook_is_drawn_first_seconds_only(self):
        ass = build_ass([], 0, 20, hook="Il a volé un avion de police", hook_seconds=3, hook_y=608)
        line = [l for l in ass.splitlines() if l.startswith("Dialogue: 1")][0]
        self.assertIn("0:00:00.00,0:00:03.00,Hook", line)
        self.assertIn("\\pos(540,608)", line)
        self.assertIn("\\N", line)  # deux lignes
        self.assertNotIn("Dialogue: 1", build_ass([], 0, 20, hook=""))


class TestImagesToAI(unittest.TestCase):
    def request(self):
        return ScoringRequest("v", "c", [Segment("a", 0, 30, "", "chat", 2.0)],
                              images={"a": [b"\xff\xd8img1", b"\xff\xd8img2"]})

    def test_images_reach_each_provider_format(self):
        cases = [
            (GeminiProvider("k"), {"candidates": [{"content": {"parts": [{"text": json.dumps(SCORES)}]}}]},
             lambda b: [p for p in b["contents"][0]["parts"] if "inlineData" in p]),
            (OpenAIProvider("k"), {"choices": [{"message": {"content": json.dumps(SCORES)}}]},
             lambda b: [c for c in b["messages"][1]["content"] if c["type"] == "image_url"]),
            (MistralProvider("k"), {"choices": [{"message": {"content": json.dumps(SCORES)}}]},
             lambda b: [c for c in b["messages"][1]["content"] if c["type"] == "image_url"]),
        ]
        for provider, resp, pick in cases:
            http = FakeHTTP(resp)
            with mock.patch.object(remote.urllib.request, "urlopen", http):
                provider.score(self.request())
            images = pick(http.requests[0]["body"])
            self.assertEqual(len(images), 2, provider.name)
            if provider.name == "mistral":
                self.assertTrue(images[0]["image_url"].startswith("data:image/jpeg;base64,"))

    def test_images_count_in_estimate_and_cache_key(self):
        p = FakeProvider()
        with_img, without = self.request(), ScoringRequest("v", "c", self.request().segments)
        self.assertGreater(p.estimate_tokens(with_img), p.estimate_tokens(without))
        self.assertNotEqual(cache_key(p, with_img), cache_key(p, without))
        result, _ = p.score(with_img)
        self.assertTrue(result.segments[0].hook)
        self.assertEqual(p.images_seen, 2)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg requis")
class TestRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.src = Path(cls.tmp.name) / "src.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=16",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-f", "lavfi",
                        "-i", "anullsrc=r=48000:cl=mono:d=4", "-f", "lavfi", "-i", "sine=frequency=660:duration=7",
                        "-filter_complex", "[1:a][2:a][3:a]concat=n=3:v=0:a=1[a]", "-map", "0:v", "-map", "[a]",
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(cls.src)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def probe(self, path):
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height",
                              "-of", "json", str(path)], capture_output=True, text=True).stdout
        data = json.loads(out)
        video = next(s for s in data["streams"] if "width" in s)
        return video["width"], video["height"], float(data["format"]["duration"])

    def test_every_layout_gives_1080x1920_and_silence_is_cut(self):
        for layout, cam in (("fit", None), ("crop", None), ("facecam", lay.Box(960, 40, 288, 162))):
            out = Path(self.tmp.name) / f"{layout}.mp4"
            render_clip(self.src, 1, 15, words_at([1.5, 2, 2.5, 10, 11]), out, layout=layout, facecam=cam,
                        trim_silences=True, hook="Regarde bien la fin", hook_seconds=2)
            w, h, d = self.probe(out)
            self.assertEqual((w, h), (1080, 1920), layout)
            self.assertLess(d, 14 - 3)  # ~4 s de silence retirées
            self.assertGreater(d, 9)

    def test_without_trim_duration_is_kept(self):
        out = Path(self.tmp.name) / "full.mp4"
        render_clip(self.src, 1, 15, [], out, layout="fit", trim_silences=False)
        self.assertAlmostEqual(self.probe(out)[2], 14, delta=0.3)


if __name__ == "__main__":
    unittest.main()
