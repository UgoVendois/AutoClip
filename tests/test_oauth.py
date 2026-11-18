import threading
import time
import unittest
import urllib.parse
import urllib.request
from unittest import mock

from autoclip import secrets
from autoclip.publishers.base import PermanentPublishError
from autoclip.publishers.youtube import ConnectFlow, OAuthCancelled


class TestConnectFlow(unittest.TestCase):
    """Le retour OAuth ne doit jamais bloquer l'app : refus, annulation, délai."""

    def setUp(self):
        secrets.use_backend(secrets.MemoryBackend())
        secrets.set_credentials("youtube", {"client_id": "id.apps.googleusercontent.com", "client_secret": "s"})

    def tearDown(self):
        secrets.use_backend(None)

    def browser_returns(self, flow, query):
        def hit():
            time.sleep(0.2)
            urllib.request.urlopen(f"{flow.flow.redirect_uri}?{query}", timeout=5).read()
        threading.Thread(target=hit, daemon=True).start()

    def test_auth_url_points_back_to_local_server(self):
        flow = ConnectFlow()
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(flow.auth_url).query)
        self.assertTrue(params["redirect_uri"][0].startswith("http://127.0.0.1:"))
        self.assertIn("https://www.googleapis.com/auth/youtube", params["scope"][0])
        flow.cancel()
        with self.assertRaises(OAuthCancelled):
            flow.wait_credentials()

    def test_access_denied_explains_test_users(self):
        flow = ConnectFlow()
        self.browser_returns(flow, "error=access_denied")
        with self.assertRaises(PermanentPublishError) as ctx:
            flow.wait_credentials()
        self.assertIn("Utilisateurs test", str(ctx.exception))

    def test_timeout_instead_of_endless_wait(self):
        flow = ConnectFlow(timeout=1.5)
        start = time.time()
        with self.assertRaises(PermanentPublishError) as ctx:
            flow.wait_credentials()
        self.assertLess(time.time() - start, 5)
        self.assertIn("access_denied", str(ctx.exception))

    def test_code_is_exchanged(self):
        flow = ConnectFlow()
        self.browser_returns(flow, "code=abc&state=" + urllib.parse.parse_qs(
            urllib.parse.urlsplit(flow.auth_url).query)["state"][0])
        with mock.patch.object(flow.flow, "fetch_token") as fetch, \
                mock.patch.object(type(flow.flow), "credentials", new_callable=mock.PropertyMock, return_value="creds"):
            self.assertEqual(flow.wait_credentials(), "creds")
        self.assertIn("code=abc", fetch.call_args.kwargs["authorization_response"])
        self.assertTrue(fetch.call_args.kwargs["authorization_response"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
