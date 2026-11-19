"""Connexion OAuth d'un compte depuis l'interface (TikTok, Instagram).

Un petit serveur local attend le retour du navigateur sur 127.0.0.1 ou
localhost. L'URL d'autorisation est connue tout de suite ; l'attente a un délai
maximum et peut être annulée (une plateforme qui refuse n'appelle pas toujours
le retour).
"""

from __future__ import annotations

import hashlib
import secrets as pysecrets
import sqlite3
import time
import urllib.parse
import wsgiref.simple_server
import wsgiref.util
from abc import ABC, abstractmethod

from .base import PermanentPublishError

CONNECT_TIMEOUT = 300
CALLBACK_PATH = "/callback/"

PAGE = ("<!doctype html><meta charset=utf-8><title>AutoClip</title>"
        "<body style='font-family:sans-serif;padding:3em;text-align:center'>"
        "<h2>%s</h2><p>Tu peux fermer cet onglet et revenir dans AutoClip.</p>")


class OAuthCancelled(Exception):
    pass


class _QuietHandler(wsgiref.simple_server.WSGIRequestHandler):
    def log_message(self, *args):  # pas de bruit dans la console
        pass


def pkce_pair(hex_challenge: bool = False) -> tuple[str, str]:
    """(code_verifier, code_challenge). TikTok (applis de bureau) attend le SHA-256
    en hexadécimal ; la norme (RFC 7636) l'attend en base64url."""
    import base64

    verifier = pysecrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode()).digest()
    if hex_challenge:
        return verifier, digest.hex()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class LoopbackFlow(ABC):
    """Autorisation par le navigateur, retour sur un serveur local, puis échange du code."""

    platform: str
    label: str
    host = "127.0.0.1"
    port = 0  # 0 = port libre choisi au démarrage

    def __init__(self, timeout: float = CONNECT_TIMEOUT) -> None:
        self._response: dict[str, str] | None = None
        self._cancelled = False
        self.deadline = time.time() + timeout
        self.timeout = timeout
        self.state = pysecrets.token_urlsafe(24)
        self.server = wsgiref.simple_server.make_server(self.host, self.port, self._wsgi,
                                                        handler_class=_QuietHandler)
        self.server.timeout = 1
        self.redirect_uri = f"http://{self.host}:{self.server.server_port}{CALLBACK_PATH}"
        self.auth_url = self.authorization_url()

    @abstractmethod
    def authorization_url(self) -> str: ...

    @abstractmethod
    def exchange(self, code: str) -> dict:
        """Code d'autorisation -> jetons (lève PermanentPublishError)."""

    @abstractmethod
    def save(self, conn: sqlite3.Connection, tokens: dict) -> int:
        """Enregistre le ou les comptes ; renvoie l'id du premier."""

    def help(self) -> str:
        return ""

    def _wsgi(self, environ, start_response):
        params = {k: v[0] for k, v in urllib.parse.parse_qs(environ.get("QUERY_STRING", "")).items()}
        on_callback = environ.get("PATH_INFO", "").rstrip("/") == CALLBACK_PATH.rstrip("/")
        if not on_callback or ("code" not in params and "error" not in params):  # favicon, etc.
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b""]
        self._response = params
        ok = "code" in params and params.get("state") == self.state
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        message = f"Compte {self.label} connecté à AutoClip" if ok else "Connexion refusée"
        return [(PAGE % message).encode()]

    def cancel(self) -> None:
        self._cancelled = True

    def wait_code(self) -> str:
        try:
            while self._response is None:
                if self._cancelled:
                    raise OAuthCancelled("connexion annulée")
                if time.time() > self.deadline:
                    raise PermanentPublishError(
                        f"Pas de réponse de {self.label} après {int(self.timeout // 60)} min. {self.help()}".strip())
                self.server.handle_request()
        finally:
            self.server.server_close()
        params = self._response
        if "error" in params:
            detail = params.get("error_description") or params.get("error_reason") or params["error"]
            raise PermanentPublishError(f"{self.label} a refusé la connexion ({detail}). {self.help()}".strip())
        if params.get("state") != self.state:
            raise PermanentPublishError("Réponse d'autorisation inattendue (state) : recommence la connexion.")
        return params["code"]

    def wait_and_save(self, conn: sqlite3.Connection) -> int:
        return self.save(conn, self.exchange(self.wait_code()))


def save_account(conn: sqlite3.Connection, platform: str, ref: str, display_name: str,
                 url: str | None) -> int:
    from ..db import tx

    with tx(conn):
        conn.execute(
            """INSERT INTO account (platform, display_name, keyring_ref, url, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (keyring_ref) DO UPDATE SET display_name = excluded.display_name,
                                                       url = excluded.url""",
            (platform, display_name, ref, url, time.time()))
        return conn.execute("SELECT id FROM account WHERE keyring_ref = ?", (ref,)).fetchone()["id"]
