import tempfile
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

from autoclip import db, secrets


@unittest.skipIf(TestClient is None, "fastapi non installé")
class TestApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        secrets.use_backend(secrets.MemoryBackend())
        from autoclip.api import create_app

        self.client = TestClient(create_app(self.home))

    def tearDown(self):
        secrets.use_backend(None)
        self.tmp.cleanup()

    def ok(self, method, path, **kwargs):
        resp = self.client.request(method, f"/api{path}", **kwargs)
        self.assertLess(resp.status_code, 300, resp.text)
        return resp.json()

    def test_approve_all_and_publish_now(self):
        from autoclip import db, services

        bot_id = self.ok("POST", "/bots", json={"name": "Clips"})["id"]
        conn = db.connect(self.home / "autoclip.db")
        services.link_account(conn, bot_id, services.create_dry_account(conn, "Chaîne"))
        src = conn.execute("INSERT INTO source (bot_id, kind, uri, status, created_at) "
                           "VALUES (?, 'file', 'x', 'done', 0)", (bot_id,)).lastrowid
        for i, score in ((1, 60.0), (2, 90.0)):
            conn.execute("INSERT INTO clip (bot_id, source_id, start_ms, end_ms, title, status, score, "
                         "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'review', ?, 0, 0)",
                         (bot_id, src, i * 1000, i * 1000 + 500, f"clip {i}", score))
        first = conn.execute("SELECT id FROM clip WHERE score = 60").fetchone()["id"]
        res = self.ok("POST", f"/bots/{bot_id}/clips/approve-all", json={str(first): {"title": "Titre revu"}})
        self.assertEqual(res["approved"], 2)
        self.assertEqual(conn.execute("SELECT title FROM clip WHERE id = ?", (first,)).fetchone()[0], "Titre revu")
        self.ok("PUT", f"/bots/{bot_id}/schedule", json={"every_hours": 4})
        pubs = self.ok("GET", f"/publish-jobs?bot_id={bot_id}")
        pending = [p for p in pubs if p["status"] == "pending"]
        self.ok("POST", f"/publish-jobs/{pending[-1]['id']}/publish-now")
        statuses = {p["id"]: p["status"] for p in self.ok("GET", f"/publish-jobs?bot_id={bot_id}")}
        self.assertEqual(statuses[pending[-1]["id"]], "queued")
        conn.close()

    def test_bot_lifecycle(self):
        bot_id = self.ok("POST", "/bots", json={"name": "Podcast"})["id"]
        self.ok("PATCH", f"/bots/{bot_id}", json={"name": "Podcast FR",
                                                  "clip_settings": {"max_clips": 3}})
        bot = self.ok("GET", f"/bots/{bot_id}")
        self.assertEqual(bot["name"], "Podcast FR")
        self.assertEqual(bot["clip_settings"]["max_clips"], 3)
        self.assertEqual(bot["clip_settings"]["max_seconds"], 60)  # valeur par défaut
        self.assertTrue(bot["dry_run"])
        self.assertEqual([b["name"] for b in self.ok("GET", "/bots")], ["Podcast FR"])
        self.ok("DELETE", f"/bots/{bot_id}")
        self.assertEqual(self.ok("GET", "/bots"), [])

    def test_validation_errors_are_400(self):
        bot_id = self.ok("POST", "/bots", json={"name": "b"})["id"]
        resp = self.client.patch(f"/api/bots/{bot_id}", json={"clip_settings": {"min_seconds": 90}})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.put(f"/api/bots/{bot_id}/schedule",
                               json={"every_hours": 2, "timezone": "Mars/Olympus"})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(f"/api/bots/{bot_id}/sources", json={"uri": "/n/existe/pas.mp4"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.client.get("/api/bots/999").status_code, 400)

    def test_schedule_and_stats(self):
        bot_id = self.ok("POST", "/bots", json={"name": "b"})["id"]
        self.ok("PUT", f"/bots/{bot_id}/schedule", json={
            "every_hours": 6, "days": "mon,fri", "window": "10:00-22:00", "timezone": "Europe/Paris"})
        sched = self.ok("GET", f"/bots/{bot_id}")["schedule"]
        self.assertEqual((sched["weekdays"], sched["window_start"], sched["window_end"]),
                         (0b0010001, "10:00", "22:00"))
        stats = self.ok("GET", f"/bots/{bot_id}/stats?days=7")
        self.assertEqual(len(stats["published_per_day"]), 7)
        self.assertIsNone(stats["next_publication"])

    def test_source_enqueues_ingest(self):
        video = self.home / "v.mp4"
        video.write_bytes(b"x")
        bot_id = self.ok("POST", "/bots", json={"name": "b"})["id"]
        self.ok("POST", f"/bots/{bot_id}/sources", json={"uri": str(video)})
        jobs = self.ok("GET", "/jobs")
        self.assertEqual([j["kind"] for j in jobs["jobs"]], ["ingest"])

    def test_review_flow_and_video(self):
        bot_id = self.ok("POST", "/bots", json={"name": "b"})["id"]
        clip_file = self.home / "clip.mp4"
        clip_file.write_bytes(b"\x00" * 1000)
        conn = db.connect(self.home / "autoclip.db")
        conn.execute("INSERT INTO source (id, bot_id, kind, uri, created_at) VALUES (1, ?, 'file', 'x', 0)",
                     (bot_id,))
        conn.execute("""INSERT INTO clip (id, bot_id, source_id, start_ms, end_ms, title, file_path,
                                          status, created_at, updated_at)
                        VALUES (1, ?, 1, 0, 30000, 'Titre', ?, 'review', 0, 0)""", (bot_id, str(clip_file)))
        conn.execute("INSERT INTO account (id, platform, display_name, created_at) VALUES (1, 'youtube', 'Chaîne', 0)")
        conn.close()

        self.assertEqual([c["id"] for c in self.ok("GET", f"/clips?bot_id={bot_id}&status=review")], [1])
        resp = self.client.get("/api/clips/1/video", headers={"Range": "bytes=0-99"})
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(len(resp.content), 100)

        # Sans compte lié, la validation est refusée avec un message clair.
        resp = self.client.post("/api/clips/1/approve", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("compte", resp.json()["detail"])

        self.ok("POST", f"/bots/{bot_id}/accounts/1")
        self.assertEqual(self.ok("POST", "/clips/1/approve", json={"title": "Nouveau titre"}),
                         {"publish_jobs": 1})
        jobs = self.ok("GET", f"/publish-jobs?bot_id={bot_id}")
        self.assertEqual((jobs[0]["title"], jobs[0]["account_name"], jobs[0]["status"]),
                         ("Nouveau titre", "Chaîne", "pending"))

    def test_settings(self):
        self.assertEqual(self.ok("GET", "/settings")["ui_theme"], "system")
        self.assertEqual(self.ok("PUT", "/settings", json={"ui_theme": "dark"})["ui_theme"], "dark")
        self.assertEqual(self.client.put("/api/settings", json={"ui_theme": "rose"}).status_code, 400)
        self.assertEqual(self.client.put("/api/settings", json={"inconnu": 1}).status_code, 400)

    def test_credentials_never_returned(self):
        self.assertFalse(self.ok("GET", "/credentials")["configured"]["youtube"])
        client_secret = {"installed": {"client_id": "id.apps.googleusercontent.com",
                                       "client_secret": "S3CRET", "project_id": "p"}}
        self.ok("PUT", "/credentials/youtube", json=client_secret)
        body = self.client.get("/api/credentials").text
        self.assertIn('"youtube":true', body.replace(" ", ""))
        self.assertNotIn("S3CRET", body)
        self.assertEqual(self.client.put("/api/credentials/tiktok", json={"client_key": "k"}).status_code, 400)
        self.ok("DELETE", "/credentials/youtube")
        self.assertFalse(self.ok("GET", "/credentials")["configured"]["youtube"])

    def test_ui_placeholder_or_index(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
