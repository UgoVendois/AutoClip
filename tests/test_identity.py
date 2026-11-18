import base64
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from autoclip import db, identity, secrets, services
from autoclip.ai import remote
from autoclip.ai.base import AIProviderError, AIRefusal, ScoringResult, json_schema
from autoclip.ai.images import FakeImage, GeminiImage, OpenAIImage, check_svg
from autoclip.ai.providers import FakeProvider
from autoclip.ai.remote import GeminiProvider, MistralProvider, OpenAIProvider
from autoclip.jobs.queue import JobQueue
from autoclip.jobs.worker import Worker
from autoclip.pipeline.cache import Cache
from autoclip.publishers.base import Account
from autoclip.publishers.channel import DryRunChannelManager, format_keywords
from autoclip.tasks import TASKS, Context

from .helpers import FakeClock

HAS_FFMPEG = shutil.which("ffmpeg") is not None
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class FakeHTTP:
    """Remplace urllib.request.urlopen : enregistre les requêtes, rejoue des réponses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        self.requests.append({"url": req.full_url, "method": req.get_method(),
                              "headers": {k.lower(): v for k, v in req.header_items()}, "body": body})
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return io.BytesIO(json.dumps(resp).encode())


def http_error(code, message="nope"):
    return urllib.error.HTTPError("https://x", code, "err", {},
                                  io.BytesIO(json.dumps({"error": {"message": message}}).encode()))


SCORES = {"segments": [{"id": "a", "score": 80, "reason": "r", "title": "t", "description": "d", "hook": "h"}]}


class TestSchema(unittest.TestCase):
    def test_refs_are_inlined_and_field_named_title_kept(self):
        schema = json_schema(ScoringResult)
        self.assertNotIn("$defs", json.dumps(schema))
        item = schema["properties"]["segments"]["items"]
        self.assertIn("title", item["properties"])
        self.assertNotIn("title", schema)  # libellé pydantic retiré

    def test_strict_closes_every_object(self):
        item = json_schema(identity.IdentityProposal, strict=True)["properties"]["playlists"]["items"]
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(set(item["required"]), {"title", "description", "keywords"})


class TestRemoteProviders(unittest.TestCase):
    def run_with(self, provider, *responses):
        http = FakeHTTP(*responses)
        with mock.patch.object(remote.urllib.request, "urlopen", http):
            result = provider.complete_json("sys", "user", ScoringResult)
        return result, http.requests

    def test_gemini_structured_output(self):
        resp = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"text": "réflexion", "thought": True}, {"text": json.dumps(SCORES)}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 5}}
        (result, usage), reqs = self.run_with(GeminiProvider("k", "gemini-x"), resp)
        self.assertEqual(result.segments[0].score, 80)
        self.assertEqual((usage.input_tokens, usage.output_tokens), (100, 25))
        req = reqs[0]
        self.assertTrue(req["url"].endswith("/models/gemini-x:generateContent"))
        self.assertEqual(req["headers"]["x-goog-api-key"], "k")
        cfg = req["body"]["generationConfig"]
        self.assertEqual(cfg["responseMimeType"], "application/json")
        self.assertIn("segments", cfg["responseJsonSchema"]["properties"])
        self.assertEqual(req["body"]["systemInstruction"]["parts"][0]["text"], "sys")

    def test_gemini_safety_block_is_a_refusal(self):
        resp = {"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]}
        with self.assertRaises(AIRefusal):
            self.run_with(GeminiProvider("k"), resp)

    def test_refusal_scores_zero_instead_of_failing(self):
        http = FakeHTTP({"candidates": [{"finishReason": "SAFETY"}]})
        from autoclip.ai.base import ScoringRequest, Segment

        req = ScoringRequest("v", "c", [Segment("a", 0, 10, "texte", "chat", 1.0)])
        with mock.patch.object(remote.urllib.request, "urlopen", http):
            result, _ = GeminiProvider("k").score(req)
        self.assertEqual(result.segments, [])

    def test_openai_uses_strict_json_schema(self):
        resp = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(SCORES)}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10}}
        (result, usage), reqs = self.run_with(OpenAIProvider("sk", "gpt-x"), resp)
        self.assertEqual(result.segments[0].id, "a")
        self.assertEqual(usage.total, 60)
        body = reqs[0]["body"]
        self.assertEqual(reqs[0]["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(reqs[0]["headers"]["authorization"], "Bearer sk")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertIn("max_completion_tokens", body)

    def test_mistral_accepts_content_blocks(self):
        resp = {"choices": [{"finish_reason": "stop", "message": {"content": [
            {"type": "text", "text": json.dumps(SCORES)}]}}], "usage": {}}
        (result, _), reqs = self.run_with(MistralProvider("m"), resp)
        self.assertEqual(result.segments[0].score, 80)
        self.assertEqual(reqs[0]["url"], "https://api.mistral.ai/v1/chat/completions")
        self.assertEqual(reqs[0]["body"]["model"], "mistral-large-latest")

    def test_auth_error_is_permanent_and_overload_is_retryable(self):
        with self.assertRaises(AIProviderError) as ctx:
            self.run_with(OpenAIProvider("bad"), http_error(401, "invalid key"))
        self.assertIn("invalid key", str(ctx.exception))
        with self.assertRaises(RuntimeError) as ctx:
            self.run_with(MistralProvider("m"), http_error(503))
        self.assertNotIsInstance(ctx.exception, AIProviderError)

    def test_list_models(self):
        http = FakeHTTP({"models": [
            {"name": "models/gemini-a", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]}]})
        with mock.patch.object(remote.urllib.request, "urlopen", http):
            self.assertEqual(GeminiProvider("k").list_models(), ["gemini-a"])


class TestImageProviders(unittest.TestCase):
    def test_gemini_image(self):
        http = FakeHTTP({"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG_1PX).decode()}}]}}]})
        with mock.patch.object(remote.urllib.request, "urlopen", http):
            img = GeminiImage("k", "gemini-3-pro-image").generate("logo", "banner")
        self.assertEqual((img.data, img.ext), (PNG_1PX, "png"))
        cfg = http.requests[0]["body"]["generationConfig"]
        self.assertEqual(cfg["responseModalities"], ["IMAGE"])
        self.assertEqual(cfg["imageConfig"], {"aspectRatio": "16:9", "imageSize": "2K"})

    def test_openai_image_sizes(self):
        http = FakeHTTP({"data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]},
                        {"data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]})
        with mock.patch.object(remote.urllib.request, "urlopen", http):
            OpenAIImage("k").generate("x", "banner")
            OpenAIImage("k", "gpt-image-1").generate("x", "banner")
        self.assertEqual([r["body"]["size"] for r in http.requests], ["2048x1152", "1536x1024"])

    def test_unsafe_svg_is_refused(self):
        ok = '<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url(#g)"/><use href="#a"/></svg>'
        self.assertEqual(check_svg(ok), ok)
        for bad in ('<svg><image href="file:///etc/passwd"/></svg>',
                    '<svg><image xlink:href="https://evil/x.png"/></svg>',
                    '<svg><script>alert(1)</script></svg>',
                    '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>',
                    '<svg><rect style="fill:url(https://evil/p)"/></svg>'):
            with self.assertRaises(RuntimeError, msg=bad):
                check_svg(bad)

    def test_keywords_format(self):
        self.assertEqual(format_keywords(["clips", "best of", 'a"b', ""]), 'clips "best of" ab')


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg requis")
class TestIdentityAgent(unittest.TestCase):
    """Proposition -> visuels -> validation -> application (dry run) -> playlists."""

    def setUp(self):
        secrets.use_backend(secrets.MemoryBackend())
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.conn = db.connect(self.home / "autoclip.db")
        self.clock = FakeClock()
        self.queue = JobQueue(self.conn, clock=self.clock)
        self.provider, self.images = FakeProvider(), FakeImage()
        self.ctx = Context(conn=self.conn, queue=self.queue, cache=Cache(self.home / "cache"), clock=self.clock,
                           ai_override=self.provider, image_override=self.images)
        self.worker = Worker(self.ctx, self.queue, TASKS)
        self.bot = services.create_bot(self.conn, "Bot", dry_run=True)
        self.account = services.create_dry_account(self.conn, "Ma chaîne")
        services.link_account(self.conn, self.bot, self.account)
        services.add_watch(self.conn, self.bot, "twitch", "Zerator", 60)

    def tearDown(self):
        secrets.use_backend(None)
        self.conn.close()
        self.tmp.cleanup()

    def published_clip(self, title):
        video = self.home / f"{title}.mp4"
        video.write_bytes(b"x")
        now = self.clock()
        with db.tx(self.conn):
            src = self.conn.execute("INSERT INTO source (bot_id, kind, uri, status, created_at) "
                                    "VALUES (?, 'file', ?, 'done', ?)", (self.bot, str(video), now)).lastrowid
            clip = self.conn.execute(
                "INSERT INTO clip (bot_id, source_id, start_ms, end_ms, title, file_path, status, created_at, "
                "updated_at) VALUES (?, ?, 0, 1000, ?, ?, 'scheduled', ?, ?)",
                (self.bot, src, title, str(video), now, now)).lastrowid
            pj = self.conn.execute("INSERT INTO publish_job (clip_id, account_id, status, created_at, updated_at) "
                                   "VALUES (?, ?, 'queued', ?, ?)", (clip, self.account, now, now)).lastrowid
            self.queue.enqueue("publish", {"publish_job_id": pj})
        self.worker.run_until_idle()
        return pj

    def dry_channel(self):
        return DryRunChannelManager(self.ctx.cache).state(Account(self.account, "youtube", "", None))

    def test_full_flow(self):
        old = self.published_clip("Zerator rage quit")  # publié avant les playlists
        ident_id = identity.start(self.conn, self.queue, self.bot, "humour, Twitch FR")
        with self.assertRaises(services.ServiceError):  # une seule proposition à la fois
            identity.start(self.conn, self.queue, self.bot)
        self.worker.run_until_idle()

        current = identity.latest(self.conn, self.bot)
        self.assertEqual((current["status"], current["images_status"]), ("ready", "ready"), current["error"])
        self.assertEqual(len(current["proposal"]["names"]), 3)
        self.assertIn("Zerator", self.provider.json_calls[0][1])  # chaînes suivies dans le contexte
        self.assertEqual(self.conn.execute("SELECT SUM(calls) FROM ai_usage").fetchone()[0], 1)
        for kind, size in (("avatar", "800x800"), ("banner", "2560x1440")):
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of",
                                  "csv=s=x:p=0", current[f"{kind}_path"]], capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), size)

        # L'utilisateur choisit un autre nom, retouche, désactive une playlist.
        playlists = current["choice"]["playlists"]
        self.assertEqual([p["title"] for p in playlists], ["Best of", "Moments de Zerator"])
        identity.update_choice(self.conn, ident_id, {"name": current["proposal"]["names"][2],
                                                     "description": "Ma description"})
        with self.assertRaises(services.ServiceError):
            identity.update_choice(self.conn, ident_id, {"description": "x" * 1001})
        identity.regenerate_images(self.conn, self.queue, ident_id)
        self.worker.run_until_idle()
        self.assertEqual(self.images.context["name"], current["proposal"]["names"][2])

        identity.apply(self.conn, self.queue, ident_id)
        self.worker.run_until_idle()
        current = identity.latest(self.conn, self.bot)
        self.assertEqual(current["status"], "applied", current["error"])
        state = self.dry_channel()
        self.assertEqual(state["branding"]["description"], "Ma description")
        self.assertTrue(state["branding"]["banner_url"].endswith(".jpg"))
        self.assertEqual(sorted(p["title"] for p in state["playlists"].values()), ["Best of", "Moments de Zerator"])
        # Le Short déjà publié a rejoint les deux playlists (mot-clé « Zerator » + best of).
        old_id = self.conn.execute("SELECT external_id FROM publish_job WHERE id = ?", (old,)).fetchone()[0]
        self.assertTrue(all(old_id in p["videos"] for p in state["playlists"].values()))

        # Nouveau Short sans le mot-clé : seulement dans « Best of ».
        new = self.published_clip("Partie de cartes")
        new_id = self.conn.execute("SELECT external_id FROM publish_job WHERE id = ?", (new,)).fetchone()[0]
        by_title = {p["title"]: p["videos"] for p in self.dry_channel()["playlists"].values()}
        self.assertIn(new_id, by_title["Best of"])
        self.assertNotIn(new_id, by_title["Moments de Zerator"])

        # Réappliquer ne recrée rien (idempotent).
        identity.apply(self.conn, self.queue, ident_id)
        self.worker.run_until_idle()
        self.assertEqual(len(self.dry_channel()["playlists"]), 2)
        self.assertEqual(len(self.dry_channel()["banners"]), 1)

    def test_apply_needs_a_youtube_account(self):
        services.unlink_account(self.conn, self.bot, self.account)
        ident_id = identity.start(self.conn, self.queue, self.bot)
        self.worker.run_until_idle()
        with self.assertRaises(services.ServiceError):
            identity.apply(self.conn, self.queue, ident_id)

    def test_generation_failure_is_reported(self):
        self.ctx.ai_override = None
        services.update_settings(self.conn, {"identity_provider": "gemini"})  # aucune clé enregistrée
        identity.start(self.conn, self.queue, self.bot)
        with self.assertLogs("autoclip.jobs.worker", level="WARNING"):
            self.worker.run_until_idle()
        current = identity.latest(self.conn, self.bot)
        self.assertEqual(current["status"], "failed")
        self.assertIn("clé d'API gemini absente", current["error"])


class TestMigrationV3(unittest.TestCase):
    def test_v2_database_gets_identity_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            import sqlite3

            path = Path(tmp) / "v2.db"
            old = sqlite3.connect(path)
            old.executescript(db.SCHEMA)
            old.executescript(db.MIGRATIONS[2])
            old.execute("PRAGMA user_version=2")
            old.commit()
            old.close()
            conn = db.connect(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            for table in ("identity", "playlist", "playlist_item"):
                conn.execute(f"SELECT * FROM {table}")
            conn.close()


if __name__ == "__main__":
    unittest.main()
