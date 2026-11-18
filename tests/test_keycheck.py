import io
import json
import unittest
import urllib.error
from unittest import mock

from autoclip import keycheck, secrets
from autoclip.ai import remote


class Response(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode())
        self.status = status


def replay(status, body):
    """urlopen simulé : 2xx -> réponse, sinon HTTPError (comme urllib)."""
    calls = []

    def urlopen(req, timeout=None):
        calls.append(req)
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(json.dumps(body).encode()))
        return Response(status, body)

    return urlopen, calls


class TestKeyCheck(unittest.TestCase):
    def setUp(self):
        secrets.use_backend(secrets.MemoryBackend())

    def tearDown(self):
        secrets.use_backend(None)

    def check(self, platform, values, status, body, module=keycheck):
        secrets.set_credentials(platform, values)
        urlopen, calls = replay(status, body)
        with mock.patch.object(module.urllib.request, "urlopen", urlopen):
            return keycheck.check(platform), calls

    def test_nothing_saved(self):
        self.assertFalse(keycheck.check("twitch").ok)

    def test_youtube_oauth_client(self):
        yt = {"client_id": "x.apps.googleusercontent.com", "client_secret": "s"}
        ok, calls = self.check("youtube", yt, 400, {"error": "invalid_grant"})
        self.assertTrue(ok.ok)
        self.assertIn(b"grant_type=authorization_code", calls[0].data)
        bad, _ = self.check("youtube", yt, 401, {"error": "invalid_client", "error_description": "Unauthorized"})
        self.assertFalse(bad.ok)
        self.assertTrue(bad.conclusive)

    def test_youtube_data_api_disabled(self):
        res, calls = self.check("youtube_data", {"api_key": "k"}, 403, {"error": {
            "message": "YouTube Data API v3 has not been used in project 123 before or it is disabled."}})
        self.assertFalse(res.ok)
        self.assertIn("pas activée", res.message)
        self.assertIn("key=k", calls[0].full_url)

    def test_twitch(self):
        tw = {"client_id": "c", "client_secret": "s"}
        self.assertTrue(self.check("twitch", tw, 200, {"access_token": "t", "expires_in": 10})[0].ok)
        bad, _ = self.check("twitch", tw, 403, {"status": 403, "message": "invalid client secret"})
        self.assertFalse(bad.ok)
        self.assertIn("invalid client secret", bad.message)

    def test_tiktok_and_meta(self):
        self.assertTrue(self.check("tiktok", {"client_key": "k", "client_secret": "s"}, 200,
                                   {"access_token": "t"})[0].ok)
        self.assertFalse(self.check("instagram", {"app_id": "a", "app_secret": "s"}, 400,
                                    {"error": {"message": "Error validating client secret."}})[0].ok)

    def test_ai_key(self):
        ok, calls = self.check("mistral", {"api_key": "m"}, 200, {"data": [{"id": "a"}, {"id": "b"}]}, module=remote)
        self.assertTrue(ok.ok)
        self.assertIn("2 modèles", ok.message)
        self.assertEqual(calls[0].full_url, "https://api.mistral.ai/v1/models")
        bad, _ = self.check("openai", {"api_key": "x"}, 401, {"error": {"message": "Incorrect API key"}}, module=remote)
        self.assertFalse(bad.ok)
        self.assertTrue(bad.conclusive)
        busy, _ = self.check("gemini", {"api_key": "g"}, 503, {"error": {"message": "overloaded"}}, module=remote)
        self.assertFalse(busy.conclusive)  # surcharge : la clé n'est pas jugée

    def test_network_error_is_not_conclusive(self):
        secrets.set_credentials("twitch", {"client_id": "c", "client_secret": "s"})
        with mock.patch.object(keycheck.urllib.request, "urlopen", side_effect=urllib.error.URLError("dns")):
            res = keycheck.check("twitch")
        self.assertFalse(res.ok)
        self.assertFalse(res.conclusive)


if __name__ == "__main__":
    unittest.main()
