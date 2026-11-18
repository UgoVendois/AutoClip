import tempfile
import unittest
from pathlib import Path

from autoclip import db, scout, secrets, services
from autoclip.ai.providers import FakeProvider
from autoclip.discovery import ChannelCandidate, SourceDiscoverer, use_discoverer
from autoclip.jobs.queue import JobQueue, PermanentError
from autoclip.jobs.worker import Worker
from autoclip.pipeline.cache import Cache
from autoclip.tasks import TASKS, Context

from .helpers import FakeClock


class FakeSearch(SourceDiscoverer):
    def __init__(self, platform, channels, fail=False):
        self.platform = platform
        self.channels = channels
        self.fail = fail
        self.queries = []

    def resolve_channel(self, channel):
        return channel

    def list_recent(self, channel_id, since):
        return []

    def search_channels(self, *, categories, keywords, language, limit=40):
        if self.fail:
            raise PermanentError("clés absentes")
        self.queries.append((categories, keywords, language))
        return list(self.channels)


def twitch(n, viewers):
    return ChannelCandidate("twitch", f"streamer{n}", f"id{n}", f"Streamer{n}", title=f"GTA 6 RP #{n}",
                            category="Grand Theft Auto VI", language="fr", viewers=viewers, live=True)


class TestScout(unittest.TestCase):
    def setUp(self):
        secrets.use_backend(secrets.MemoryBackend())
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "a.db")
        self.clock = FakeClock()
        self.queue = JobQueue(self.conn, clock=self.clock)
        self.provider = FakeProvider()
        self.ctx = Context(conn=self.conn, queue=self.queue, cache=Cache(Path(self.tmp.name) / "c"),
                           clock=self.clock, ai_override=self.provider)
        self.worker = Worker(self.ctx, self.queue, TASKS)
        self.bot = services.create_bot(self.conn, "GTA", dry_run=True)
        services.update_bot(self.conn, self.bot, ai_settings={
            "provider": "fake", "scout_enabled": True, "scout_theme": "GTA 6, streamers FR", "scout_max_channels": 3})
        self.tw = FakeSearch("twitch", [twitch(i, v) for i, v in enumerate([50, 9000, 300, 1200, 20])])
        self.yt = FakeSearch("youtube", [ChannelCandidate("youtube", "UCaaaaaaaaaaaaaaaaaaaaaa", "UCaaaaaaaaaaaaaaaaaaaaaa",
                                                          "GTA News FR", title="GTA 6 : tout savoir", viewers=100)])
        use_discoverer("twitch", self.tw)
        use_discoverer("youtube", self.yt)

    def tearDown(self):
        use_discoverer("twitch", None)
        use_discoverer("youtube", None)
        secrets.use_backend(None)
        self.conn.close()
        self.tmp.cleanup()

    def ai_watches(self):
        return self.conn.execute("SELECT * FROM watch WHERE origin = 'ai' AND dismissed = 0 ORDER BY id").fetchall()

    def run_scout(self):
        scout.start(self.conn, self.queue, self.bot, now=self.clock())
        self.clock.advance(1)
        self.worker.run_until_idle()

    def test_ai_plans_searches_and_picks_channels(self):
        self.run_scout()
        # Recherche construite par l'IA à partir du thème, langue transmise.
        self.assertEqual(self.tw.queries[0][2], "fr")
        watches = self.ai_watches()
        self.assertEqual(len(watches), 3)  # limite scout_max_channels
        self.assertEqual(watches[0]["channel"], "streamer1")  # le plus regardé d'abord (faux fournisseur)
        self.assertTrue(all(w["reason"] and w["channel_id"] for w in watches))
        run = self.conn.execute("SELECT * FROM scout_run").fetchone()
        self.assertEqual((run["status"], run["candidates"]), ("done", 6))
        # Deux appels à l'IA (plan + choix), comptés dans le budget.
        self.assertEqual([c[2].__name__ for c in self.provider.json_calls], ["ScoutPlan", "ScoutPick"])
        self.assertEqual(self.conn.execute("SELECT SUM(calls) FROM ai_usage").fetchone()[0], 2)

    def test_full_then_removed_channel_is_never_proposed_again(self):
        self.run_scout()
        self.run_scout()  # complet : rien de plus
        self.assertEqual(len(self.ai_watches()), 3)
        removed = self.ai_watches()[0]
        services.remove_watch(self.conn, removed["id"])
        self.run_scout()
        channels = [w["channel"] for w in self.ai_watches()]
        self.assertEqual(len(channels), 3)
        self.assertNotIn(removed["channel"], channels)
        # La chaîne écartée n'est plus vérifiée par la découverte.
        from autoclip.discovery_tasks import enqueue_due_discoveries
        self.assertEqual(enqueue_due_discoveries(self.conn, self.queue, self.clock()), 3)

    def test_manual_watch_is_not_duplicated(self):
        services.add_watch(self.conn, self.bot, "twitch", "streamer1")
        self.run_scout()
        self.assertNotIn("streamer1", [w["channel"] for w in self.ai_watches()])

    def test_without_ai_most_watched_win(self):
        self.ctx.ai_override = None
        services.update_bot(self.conn, self.bot, ai_settings={"provider": "none"})
        self.run_scout()
        self.assertEqual([w["channel"] for w in self.ai_watches()], ["streamer1", "streamer3", "streamer2"])
        self.assertEqual(self.tw.queries[0][0], ["GTA 6, streamers FR"])  # thème utilisé tel quel

    def test_one_platform_without_keys_does_not_block_the_other(self):
        self.yt.fail = True
        self.run_scout()
        run = self.conn.execute("SELECT * FROM scout_run").fetchone()
        self.assertEqual(run["status"], "done")
        self.assertIn("youtube", run["error"])
        self.tw.fail = True
        self.run_scout()
        self.assertEqual(self.conn.execute("SELECT status FROM scout_run ORDER BY id DESC").fetchone()[0], "failed")

    def test_periodic_scouting(self):
        now = self.clock()
        self.assertEqual(scout.enqueue_due_scouts(self.conn, self.queue, now), 1)
        self.worker.run_until_idle()
        self.assertEqual(scout.enqueue_due_scouts(self.conn, self.queue, now + 3600), 0)  # 24 h par défaut
        self.assertEqual(scout.enqueue_due_scouts(self.conn, self.queue, now + 25 * 3600), 1)

    def test_ai_out_of_credits_still_finds_channels(self):
        from autoclip.ai.base import AIProviderError

        class Broke(FakeProvider):
            def complete_json(self, *a, **k):
                raise AIProviderError("Anthropic : plus de crédits API.")

        self.ctx.ai_override = Broke()
        self.run_scout()
        run = self.conn.execute("SELECT * FROM scout_run").fetchone()
        self.assertEqual(run["status"], "done")
        self.assertIn("crédits", run["error"])
        self.assertEqual(len(self.ai_watches()), 3)

    def test_theme_is_required(self):
        with self.assertRaises(services.ServiceError):
            services.update_bot(self.conn, self.bot, ai_settings={"scout_theme": " "})


class TestTwitchSearch(unittest.TestCase):
    def test_streams_of_matching_category_then_keyword_channels(self):
        from autoclip.discovery.twitch import TwitchDiscoverer

        calls = []

        def get(path, **params):
            calls.append((path, params))
            if path == "/search/categories":
                return {"data": [{"id": "1", "name": "GTA V"}, {"id": "2", "name": "Grand Theft Auto VI"}]}
            if path == "/streams":
                return {"data": [{"user_id": "10", "user_login": "a", "user_name": "A", "title": "RP",
                                  "game_name": "Grand Theft Auto VI", "language": "fr", "viewer_count": 500}]}
            if path == "/search/channels":
                return {"data": [{"id": "11", "broadcaster_login": "b", "display_name": "B", "title": "GTA 6",
                                  "game_name": "Grand Theft Auto VI", "broadcaster_language": "fr", "is_live": False},
                                 {"id": "12", "broadcaster_login": "c", "broadcaster_language": "en", "is_live": True}]}
            raise AssertionError(path)

        disc = TwitchDiscoverer.__new__(TwitchDiscoverer)
        disc.helix = type("H", (), {"get": staticmethod(get)})()
        found = disc.search_channels(categories=["Grand Theft Auto VI"], keywords=["gta 6"], language="fr")
        self.assertEqual([c.channel for c in found], ["a", "b"])  # live d'abord, chaîne anglaise écartée
        self.assertEqual(calls[1], ("/streams", {"game_id": "2", "language": "fr", "first": 50}))


if __name__ == "__main__":
    unittest.main()
