"""Connecteur YouTube Shorts (YouTube Data API v3, upload résumable).

Prérequis : un projet Google Cloud avec l'API YouTube Data v3 activée et un
identifiant OAuth de type « Application de bureau ». Tant que Google n'a pas
audité le projet, les vidéos envoyées par l'API restent privées.
"""

from __future__ import annotations

import json
import sqlite3
import time

from .. import secrets
from ..db import tx
from .base import (Account, PermanentPublishError, Publisher, PublishRequest, PublishResult,
                   RetryablePublishError)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    # Gestion de la chaîne (bannière, description, playlists) : agent identité.
    "https://www.googleapis.com/auth/youtube",
]
MANAGE_SCOPE = SCOPES[2]


def has_manage_scope(keyring_ref: str | None) -> bool:
    """Le jeton enregistré autorise-t-il la gestion de la chaîne ?"""
    info = secrets.get_json(keyring_ref or "") or {}
    return MANAGE_SCOPE in (info.get("scopes") or [])
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
RETRYABLE_REASONS = {"quotaExceeded", "rateLimitExceeded", "userRateLimitExceeded", "backendError"}
CHUNK = 8 * 1024 * 1024


def _client_config() -> dict:
    creds = secrets.get_json(secrets.credentials_key("youtube"))
    if not creds:
        raise PermanentPublishError(
            "clés YouTube absentes : `autoclip credentials set youtube client_secret.json`")
    return {"installed": {
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}


HELP_403 = ("Si Google a affiché « Erreur 403 : access_denied », ton projet Google Cloud est en mode Test : "
            "ajoute ton adresse Gmail dans Google Auth Platform > Audience > Utilisateurs test, puis réessaie. "
            "« Erreur 403 : org_internal » : l'application est réservée à une organisation (type Interne) : "
            "passe-la en Externe dans Google Auth Platform > Audience.")
CONNECT_TIMEOUT = 300


class OAuthCancelled(Exception):
    pass


class ConnectFlow:
    """Connexion OAuth d'une chaîne, pilotable depuis l'interface : l'URL Google
    est connue tout de suite, l'attente du retour a un délai maximum et peut être
    annulée (Google n'appelle jamais le retour quand il refuse l'accès)."""

    SUCCESS_PAGE = ("<!doctype html><meta charset=utf-8><title>AutoClip</title>"
                    "<body style='font-family:sans-serif;padding:3em;text-align:center'>"
                    "<h2>%s</h2><p>Tu peux fermer cet onglet et revenir dans AutoClip.</p>")

    def __init__(self, timeout: float = CONNECT_TIMEOUT) -> None:
        import wsgiref.simple_server

        from google_auth_oauthlib.flow import InstalledAppFlow

        self.flow = InstalledAppFlow.from_client_config(_client_config(), SCOPES)
        self._response: str | None = None
        self._cancelled = False
        self.deadline = time.time() + timeout

        class QuietHandler(wsgiref.simple_server.WSGIRequestHandler):
            def log_message(self, *args):  # pas de bruit dans la console
                pass

        self.server = wsgiref.simple_server.make_server("127.0.0.1", 0, self._wsgi, handler_class=QuietHandler)
        self.server.timeout = 1
        self.flow.redirect_uri = f"http://127.0.0.1:{self.server.server_port}/"
        self.auth_url, _ = self.flow.authorization_url(prompt="consent", access_type="offline")

    def _wsgi(self, environ, start_response):
        import wsgiref.util

        query = environ.get("QUERY_STRING", "")
        if "code=" not in query and "error=" not in query:  # favicon, etc.
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b""]
        self._response = wsgiref.util.request_uri(environ)
        ok = "code=" in query
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        message = "Chaîne connectée à AutoClip" if ok else "Connexion refusée"
        return [(self.SUCCESS_PAGE % message).encode()]

    def cancel(self) -> None:
        self._cancelled = True

    def wait_credentials(self):
        """Attend le retour de Google ; lève OAuthCancelled ou PermanentPublishError."""
        import urllib.parse

        try:
            while self._response is None:
                if self._cancelled:
                    raise OAuthCancelled("connexion annulée")
                if time.time() > self.deadline:
                    raise PermanentPublishError(f"Pas de réponse de Google après {CONNECT_TIMEOUT // 60} min. " + HELP_403)
                self.server.handle_request()
        finally:
            self.server.server_close()
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(self._response).query)
        if "error" in params:
            error = params["error"][0]
            if error == "access_denied":
                raise PermanentPublishError("Accès refusé sur la page Google. " + HELP_403)
            raise PermanentPublishError(f"Google a refusé la connexion ({error}).")
        # oauthlib exige https, même pour le retour local.
        self.flow.fetch_token(authorization_response=self._response.replace("http://", "https://", 1))
        return self.flow.credentials


def save_connected_account(conn: sqlite3.Connection, creds) -> int:
    """Enregistre le jeton dans le trousseau et crée (ou met à jour) le compte."""
    from googleapiclient.discovery import build

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    channels = youtube.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not channels:
        raise PermanentPublishError(
            "Google a bien connecté le compte, mais il n'a pas de chaîne YouTube. Si ta chaîne existe, elle est "
            "sans doute sur un compte de marque : à la connexion, choisis le nom de la chaîne dans la liste "
            "« Choisir un compte ou une chaîne », pas ton compte personnel. Sinon, crée la chaîne sur "
            "youtube.com (photo de profil > Créer une chaîne), puis réessaie.")
    channel = channels[0]
    ref = f"token:youtube:{channel['id']}"
    secrets.set(ref, creds.to_json())
    with tx(conn):
        conn.execute(
            """INSERT INTO account (platform, display_name, keyring_ref, created_at)
               VALUES ('youtube', ?, ?, ?)
               ON CONFLICT (keyring_ref) DO UPDATE SET display_name = excluded.display_name""",
            (channel["snippet"]["title"], ref, time.time()),
        )
        return conn.execute("SELECT id FROM account WHERE keyring_ref = ?", (ref,)).fetchone()["id"]


def connect_account(conn: sqlite3.Connection, open_browser: bool = True) -> int:
    """Connexion bloquante (CLI) : ouvre la page Google et attend le retour."""
    import webbrowser

    flow = ConnectFlow()
    print(f"Page de connexion Google : {flow.auth_url}")
    if open_browser:
        webbrowser.open(flow.auth_url, new=1)
    return save_connected_account(conn, flow.wait_credentials())


def _marker(token: str) -> str:
    return f"autoclip-{token}"


class YouTubePublisher(Publisher):
    platform = "youtube"

    def _service(self, account: Account):
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        info = secrets.get_json(account.keyring_ref or "")
        if not info:
            raise PermanentPublishError(f"compte {account.display_name} non connecté")
        # Droits du jeton tels qu'accordés (un ancien jeton n'a pas MANAGE_SCOPE).
        creds = Credentials.from_authorized_user_info(info)
        if not creds.valid:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise PermanentPublishError(
                    f"connexion expirée pour {account.display_name} : reconnecte le compte") from exc
            secrets.set(account.keyring_ref, creds.to_json())
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    def _call(self, request):
        from googleapiclient.errors import HttpError

        try:
            return request.execute()
        except HttpError as exc:
            raise self._map_error(exc) from exc

    @staticmethod
    def _map_error(exc) -> Exception:
        status = exc.resp.status
        try:
            reasons = {e.get("reason") for e in json.loads(exc.content)["error"]["errors"]}
        except Exception:  # noqa: BLE001
            reasons = set()
        if status in RETRYABLE_STATUS or reasons & RETRYABLE_REASONS:
            return RetryablePublishError(f"YouTube {status} {sorted(reasons)}")
        return PermanentPublishError(f"YouTube {status} {sorted(reasons)} : {exc}")

    def find_existing(self, req: PublishRequest, account: Account) -> PublishResult | None:
        yt = self._service(account)
        channels = self._call(yt.channels().list(part="contentDetails", mine=True))
        uploads = channels["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        items = self._call(yt.playlistItems().list(part="contentDetails", playlistId=uploads,
                                                   maxResults=50)).get("items", [])
        ids = [i["contentDetails"]["videoId"] for i in items]
        if not ids:
            return None
        videos = self._call(yt.videos().list(part="snippet", id=",".join(ids)))
        for video in videos.get("items", []):
            if _marker(req.token) in video["snippet"].get("tags", []):
                return PublishResult(video["id"], f"https://youtube.com/shorts/{video['id']}")
        return None

    def publish(self, req: PublishRequest, account: Account) -> PublishResult:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        yt = self._service(account)
        title = req.title.strip() or "Short"
        if "#shorts" not in title.lower() and len(title) + 8 <= 100:
            title += " #Shorts"
        body = {
            "snippet": {
                "title": title[:100],
                "description": req.description[:5000],
                "tags": [*req.tags, _marker(req.token)],
                "categoryId": "22",
                # Langue des titres et de l'audio : YouTube propose la vidéo au bon public.
                **({"defaultLanguage": req.language, "defaultAudioLanguage": req.language}
                   if req.language else {}),
            },
            "status": {"privacyStatus": req.privacy, "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(str(req.video_path), mimetype="video/mp4", chunksize=CHUNK,
                                resumable=True)
        request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        try:
            while response is None:
                _, response = request.next_chunk(num_retries=3)
        except HttpError as exc:
            raise self._map_error(exc) from exc
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise RetryablePublishError(f"réseau : {exc}") from exc
        video_id = response["id"]
        return PublishResult(video_id, f"https://youtube.com/shorts/{video_id}")
