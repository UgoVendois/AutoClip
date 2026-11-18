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
]
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


def connect_account(conn: sqlite3.Connection, open_browser: bool = True) -> int:
    """Ouvre la page de connexion Google, enregistre le token dans le trousseau
    et crée (ou met à jour) le compte. Renvoie l'id du compte."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    flow = InstalledAppFlow.from_client_config(_client_config(), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=open_browser,
                                  prompt="consent", access_type="offline")
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    channels = youtube.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not channels:
        raise PermanentPublishError("ce compte Google n'a pas de chaîne YouTube")
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
        creds = Credentials.from_authorized_user_info(info, SCOPES)
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
