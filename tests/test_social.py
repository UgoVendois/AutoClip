"""Connecteurs TikTok et Instagram : connexion OAuth et publication (HTTP simulé)."""

import hashlib
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from autoclip import db, secrets
from autoclip.pipeline.cache import Cache
from autoclip.publishers import Account, PermanentPublishError, PublishRequest, RetryablePublishError
from autoclip.publishers import instagram, tiktok
from autoclip.publishers.http import HTTPStatusError, Response


class FakeAPI:
    """Remplace `request` d'un connecteur : enregistre les appels, rejoue des réponses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp if isinstance(resp, Response) else Response(200, resp)

    def urls(self):
        return [c["url"].split("?")[0] for c in self.calls]


def ok(data):
    return {"data": data, "error": {"code": "ok", "message": "", "log_id": "x"}}


class SocialCase(unittest.TestCase):
    def setUp(self):
        secrets.use_backend(secrets.MemoryBackend())
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cache = Cache(self.home / "cache")
        self.video = self.home / "clip.mp4"
        self.video.write_bytes(b"v" * 1000)
        self.req = PublishRequest(video_path=self.video, title="Il ne s'attendait pas à ça",
                                  description="Le moment du live.", token="c1a2", tags=["gta6", "clips fr"])

    def tearDown(self):
        secrets.use_backend(None)
        self.tmp.cleanup()


# -- TikTok -------------------------------------------------------------------------------

class TestTikTokChunks(unittest.TestCase):
    def test_small_and_medium_videos_go_in_one_piece(self):
        self.assertEqual(tiktok.chunk_plan(4_000_000), (4_000_000, 1))
        self.assertEqual(tiktok.chunk_plan(50_000_000), (50_000_000, 1))
        self.assertEqual(tiktok.chunk_ranges(50_000_000), [(0, 49_999_999)])

    def test_large_video_last_chunk_takes_the_rest(self):
        size = 150_000_123
        self.assertEqual(tiktok.chunk_plan(size), (10_000_000, 15))
        ranges = tiktok.chunk_ranges(size)
        self.assertEqual(len(ranges), 15)
        self.assertEqual(ranges[0], (0, 9_999_999))
        self.assertEqual(ranges[-1], (140_000_000, size - 1))  # 10 000 123 octets, moins de 128 Mo


class TestTikTokPublish(SocialCase):
    def setUp(self):
        super().setUp()
        secrets.set_credentials("tiktok", {"client_key": "ck", "client_secret": "cs"})
        self.account = Account(7, "tiktok", "Moi", "token:tiktok:o1")
        secrets.set_json("token:tiktok:o1", {"access_token": "at", "refresh_token": "rt",
                                             "expires_at": time.time() + 3600})

    def publisher(self, privacy="PUBLIC_TO_EVERYONE"):
        return tiktok.TikTokPublisher(self.cache, privacy=privacy, sleep=lambda s: None)

    def test_direct_post_uploads_then_waits_for_publication(self):
        api = FakeAPI(
            ok({"creator_username": "moi", "privacy_level_options": ["SELF_ONLY"]}),
            ok({"publish_id": "p1", "upload_url": "https://upload.tiktok/u1"}),
            Response(201, {}),
            ok({"status": "PROCESSING_UPLOAD"}),
            ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [123]}),
        )
        with mock.patch.object(tiktok, "request", api):
            result = self.publisher().publish_once(self.req, self.account)
        self.assertEqual(result.url, "https://www.tiktok.com/@moi/video/123")
        init = api.calls[1]["json_body"]
        # Réglage « public » mais compte non audité : seule l'option privée est permise.
        self.assertEqual(init["post_info"]["privacy_level"], "SELF_ONLY")
        self.assertIn("#gta6 #clipsfr", init["post_info"]["title"])
        self.assertEqual(init["source_info"], {"source": "FILE_UPLOAD", "video_size": 1000,
                                               "chunk_size": 1000, "total_chunk_count": 1})
        put = api.calls[2]
        self.assertEqual((put["method"], put["url"]), ("PUT", "https://upload.tiktok/u1"))
        self.assertEqual(put["headers"]["Content-Range"], "bytes 0-999/1000")
        self.assertEqual(api.calls[0]["headers"]["Authorization"], "Bearer at")

    def test_resume_after_crash_does_not_post_twice(self):
        Cache.write_json(self.cache.path("publish", "tiktok-c1a2.json"),
                         {"publish_id": "p1", "upload_url": "u", "creator": "moi", "uploaded": True,
                          "created_at": time.time()})
        api = FakeAPI(ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": []}))
        with mock.patch.object(tiktok, "request", api):
            result = self.publisher().publish_once(self.req, self.account)
        self.assertEqual(api.urls(), [f"{tiktok.API}/post/publish/status/fetch/"])
        self.assertEqual((result.external_id, result.url), ("p1", "https://www.tiktok.com/@moi"))

    def test_expired_token_is_refreshed_and_saved(self):
        secrets.set_json("token:tiktok:o1", {"access_token": "old", "refresh_token": "rt",
                                             "expires_at": time.time() - 10})
        api = FakeAPI({"access_token": "new", "refresh_token": "rt2", "expires_in": 86400, "open_id": "o1"},
                      ok({"creator_username": "moi", "privacy_level_options": ["SELF_ONLY"]}),
                      HTTPStatusError(403, {"error": {"code": "unaudited_client_can_only_post_to_private_accounts",
                                                      "message": "x"}}, ""))
        with mock.patch.object(tiktok, "request", api):
            with self.assertRaises(PermanentPublishError) as ctx:
                self.publisher().publish(self.req, self.account)
        self.assertIn("compte privé", str(ctx.exception))
        self.assertEqual(api.calls[0]["form"]["grant_type"], "refresh_token")
        stored = secrets.get_json("token:tiktok:o1")
        self.assertEqual((stored["access_token"], stored["refresh_token"]), ("new", "rt2"))
        self.assertEqual(api.calls[1]["headers"]["Authorization"], "Bearer new")

    def test_rejected_video_is_permanent_and_internal_error_retried(self):
        for reason, error in (("duration_check_failed", PermanentPublishError), ("internal", RetryablePublishError)):
            api = FakeAPI(ok({"creator_username": "moi", "privacy_level_options": ["SELF_ONLY"]}),
                          ok({"publish_id": "p", "upload_url": "u"}), Response(201, {}),
                          ok({"status": "FAILED", "fail_reason": reason}))
            with mock.patch.object(tiktok, "request", api), self.assertRaises(error):
                self.publisher().publish_once(self.req, self.account)
            self.assertFalse(self.cache.path("publish", "tiktok-c1a2.json").exists())


class TestTikTokConnect(SocialCase):
    def test_desktop_pkce_and_account_saved(self):
        secrets.set_credentials("tiktok", {"client_key": "ck", "client_secret": "cs"})
        flow = tiktok.TikTokConnect(timeout=10)
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(flow.auth_url).query))
        self.assertEqual(params["code_challenge"], hashlib.sha256(flow.verifier.encode()).hexdigest())
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertTrue(flow.redirect_uri.startswith("http://127.0.0.1:"))
        self.assertTrue(flow.redirect_uri.endswith("/callback/"))
        self.assertEqual(params["scope"], tiktok.SCOPES)

        callback = f"{flow.redirect_uri}?code=abc&state={params['state']}"
        threading.Timer(0.2, lambda: urllib.request.urlopen(callback, timeout=5).read()).start()
        api = FakeAPI({"access_token": "at", "refresh_token": "rt", "expires_in": 86400, "open_id": "o9"},
                      {"data": {"user": {"display_name": "Clips GTA", "username": "clipsgta"}}})
        conn = db.connect(self.home / "a.db")
        with mock.patch.object(tiktok, "request", api):
            account_id = flow.wait_and_save(conn)
        row = conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
        self.assertEqual((row["platform"], row["display_name"], row["url"]),
                         ("tiktok", "Clips GTA (@clipsgta)", "https://www.tiktok.com/@clipsgta"))
        self.assertEqual(api.calls[0]["form"]["code_verifier"], flow.verifier)
        self.assertEqual(api.calls[0]["form"]["redirect_uri"], flow.redirect_uri)
        self.assertEqual(secrets.get_json("token:tiktok:o9")["refresh_token"], "rt")
        conn.close()


# -- Instagram -----------------------------------------------------------------------------

class TestInstagramPublish(SocialCase):
    def setUp(self):
        super().setUp()
        self.account = Account(3, "instagram", "@moi", "token:instagram:17841")
        secrets.set_json("token:instagram:17841", {"page_token": "pt", "ig_user_id": "17841", "username": "moi"})

    def publisher(self):
        return instagram.InstagramPublisher(self.cache, sleep=lambda s: None)

    def test_reel_container_upload_wait_publish(self):
        api = FakeAPI({"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v27.0/c1"},
                      {"success": True},
                      {"status_code": "IN_PROGRESS"},
                      {"status_code": "FINISHED"},
                      {"id": "m1"},
                      {"permalink": "https://www.instagram.com/reel/abc/"})
        with mock.patch.object(instagram, "request", api):
            result = self.publisher().publish_once(self.req, self.account)
        self.assertEqual((result.external_id, result.url), ("m1", "https://www.instagram.com/reel/abc/"))
        create = api.calls[0]
        self.assertEqual(create["url"], f"{instagram.GRAPH}/17841/media")
        self.assertEqual((create["params"]["media_type"], create["params"]["upload_type"]), ("REELS", "resumable"))
        self.assertIn("#gta6 #clipsfr", create["params"]["caption"])
        upload = api.calls[1]
        self.assertEqual(upload["headers"], {"Authorization": "OAuth pt", "offset": "0", "file_size": "1000"})
        self.assertEqual(upload["data"], b"v" * 1000)
        self.assertEqual(api.calls[4]["params"]["creation_id"], "c1")

    def test_resume_publishes_finished_container_once(self):
        Cache.write_json(self.cache.path("publish", "instagram-c1a2.json"), {"container_id": "c1", "uploaded": True})
        api = FakeAPI({"status_code": "FINISHED"}, {"id": "m1"}, {"permalink": "https://www.instagram.com/reel/x/"})
        with mock.patch.object(instagram, "request", api):
            result = self.publisher().publish_once(self.req, self.account)
        self.assertEqual(result.external_id, "m1")
        self.assertNotIn(f"{instagram.GRAPH}/17841/media", api.urls())  # pas de nouveau conteneur
        # Rejoué une fois de plus : le média est retrouvé sans rien republier.
        api = FakeAPI({"permalink": "https://www.instagram.com/reel/x/"})
        with mock.patch.object(instagram, "request", api):
            again = self.publisher().publish_once(self.req, self.account)
        self.assertEqual((again.external_id, len(api.calls)), ("m1", 1))

    def test_errors_are_classified(self):
        expired = HTTPStatusError(400, {"error": {"code": 190, "message": "token expired"}}, "")
        with mock.patch.object(instagram, "request", FakeAPI(expired)):
            with self.assertRaises(PermanentPublishError) as ctx:
                self.publisher().publish(self.req, self.account)
        self.assertIn("reconnecte", str(ctx.exception))
        limited = HTTPStatusError(400, {"error": {"code": 4, "message": "rate"}}, "")
        with mock.patch.object(instagram, "request", FakeAPI(limited)), self.assertRaises(RetryablePublishError):
            self.publisher().publish(self.req, self.account)
        api = FakeAPI({"id": "c2"}, {"success": True}, {"status_code": "ERROR", "status": "2207026"})
        with mock.patch.object(instagram, "request", api), self.assertRaises(PermanentPublishError):
            self.publisher().publish(self.req, self.account)


class TestInstagramConnect(SocialCase):
    def setUp(self):
        super().setUp()
        secrets.set_credentials("instagram", {"app_id": "app", "app_secret": "sec"})

    def test_pages_with_instagram_accounts_are_saved(self):
        flow = instagram.InstagramConnect(timeout=10)
        try:
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(flow.auth_url).query))
            self.assertEqual(flow.redirect_uri, "http://localhost:8767/callback/")
            self.assertEqual(params["scope"], instagram.SCOPES)
            api = FakeAPI({"data": [
                {"id": "pg1", "name": "Page", "access_token": "pt1",
                 "instagram_business_account": {"id": "17841", "username": "clipsgta"}},
                {"id": "pg2", "name": "Sans Instagram", "access_token": "pt2"}]})
            conn = db.connect(self.home / "a.db")
            with mock.patch.object(instagram, "request", api):
                account_id = flow.save(conn, {"access_token": "long"})
            row = conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
            self.assertEqual((row["display_name"], row["url"]), ("@clipsgta", "https://www.instagram.com/clipsgta/"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM account").fetchone()[0], 1)
            self.assertEqual(secrets.get_json("token:instagram:17841")["page_token"], "pt1")
            with mock.patch.object(instagram, "request", FakeAPI({"data": [{"id": "pg2", "access_token": "x"}]})):
                with self.assertRaises(PermanentPublishError) as ctx:
                    flow.save(conn, {"access_token": "long"})
            self.assertIn("professionnel", str(ctx.exception))
            conn.close()
        finally:
            flow.server.server_close()

    def test_config_id_replaces_scopes(self):
        secrets.set_credentials("instagram", {"app_id": "app", "app_secret": "sec", "config_id": "cfg"})
        flow = instagram.InstagramConnect(timeout=10)
        flow.server.server_close()
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(flow.auth_url).query))
        self.assertEqual(params["config_id"], "cfg")
        self.assertNotIn("scope", params)

    def test_code_exchange_gets_long_lived_token(self):
        flow = instagram.InstagramConnect(timeout=10)
        flow.server.server_close()
        api = FakeAPI({"access_token": "short"}, {"access_token": "long", "expires_in": 5183944})
        with mock.patch.object(instagram, "request", api):
            tokens = flow.exchange("code")
        self.assertEqual(tokens["access_token"], "long")
        self.assertEqual(api.calls[0]["params"]["redirect_uri"], "http://localhost:8767/callback/")
        self.assertEqual(api.calls[1]["params"]["grant_type"], "fb_exchange_token")


if __name__ == "__main__":
    unittest.main()
