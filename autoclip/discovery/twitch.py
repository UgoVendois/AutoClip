"""Découverte Twitch (API Helix officielle) et enregistrement du chat en direct.

Twitch ne fournit aucune API officielle pour le chat d'une VOD passée. Le chat
est donc enregistré pendant le live via EventSub (`channel.chat.message`), avec
le jeton d'un compte Twitch quelconque (portée `user:read:chat`) obtenu par
Device Code Flow. Une VOD sans chat enregistré se rabat sur l'énergie audio.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from .. import secrets
from ..db import tx
from ..jobs.queue import PermanentError
from .base import ChannelCandidate, DiscoveredItem, Signal, SourceDiscoverer, register

log = logging.getLogger(__name__)

HELIX = "https://api.twitch.tv/helix"
ID = "https://id.twitch.tv/oauth2"
EVENTSUB_WS = "wss://eventsub.wss.twitch.tv/ws"
USER_TOKEN_REF = "token:twitch:user"
SCOPES = "user:read:chat"
MIN_CHAT_MESSAGES = 50


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def parse_duration(text: str) -> float:
    """Durée Helix « 3h2m10s » -> secondes."""
    total = 0
    for value, unit in re.findall(r"(\d+)([hms])", text):
        total += int(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return float(total)


def _app_credentials() -> dict:
    creds = secrets.get_json(secrets.credentials_key("twitch"))
    if not creds:
        raise PermanentError("clés Twitch absentes : `autoclip credentials set twitch`")
    return creds


def _post_form(url: str, fields: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


class HelixClient:
    """Appels Helix avec un jeton d'application (client credentials)."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def _app_token(self) -> str:
        with self._lock:
            if not self._token or time.time() > self._expires - 60:
                creds = _app_credentials()
                data = _post_form(f"{ID}/token", {"client_id": creds["client_id"],
                                                   "client_secret": creds["client_secret"],
                                                   "grant_type": "client_credentials"})
                self._token, self._expires = data["access_token"], time.time() + data["expires_in"]
            return self._token

    def get(self, path: str, **params) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
        req = urllib.request.Request(f"{HELIX}{path}?{query}", headers={
            "Client-Id": _app_credentials()["client_id"],
            "Authorization": f"Bearer {self._app_token()}",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self._token = None
            if exc.code in (400, 404):
                raise PermanentError(f"Twitch {exc.code} : {exc.read().decode()[:300]}") from exc
            raise


@register
class TwitchDiscoverer(SourceDiscoverer):
    platform = "twitch"

    def __init__(self) -> None:
        self.helix = HelixClient()

    def resolve_channel(self, channel: str) -> str:
        login = channel.strip().rstrip("/").split("/")[-1].lstrip("@").lower()
        users = self.helix.get("/users", login=login).get("data", [])
        if not users:
            raise PermanentError(f"chaîne Twitch introuvable : {channel}")
        return users[0]["id"]

    def list_recent(self, channel_id: str, since: float) -> list[DiscoveredItem]:
        videos = self.helix.get("/videos", user_id=channel_id, type="archive", first=20).get("data", [])
        live = {s["id"] for s in self.helix.get("/streams", user_id=channel_id).get("data", [])}
        items = []
        for v in videos:
            created = _ts(v["created_at"])
            if created <= since:
                continue
            items.append(DiscoveredItem(
                external_id=v["id"], url=v["url"], title=v["title"], published_at=created,
                duration=parse_duration(v["duration"]), channel_id=channel_id,
                stream_id=v.get("stream_id"), live=v.get("stream_id") in live,
            ))
        return items

    def signal(self, conn: sqlite3.Connection, item: DiscoveredItem) -> Signal | None:
        if not item.stream_id:
            return None
        rows = conn.execute(
            "SELECT ts FROM chat_message WHERE platform = 'twitch' AND stream_id = ? ORDER BY ts",
            (item.stream_id,)).fetchall()
        if len(rows) < MIN_CHAT_MESSAGES:
            return None
        # La VOD commence au début du live : created_at de la VOD = instant 0.
        events = [r["ts"] - item.published_at for r in rows]
        return Signal("chat", [e for e in events if e >= 0])

    def search_channels(self, *, categories: list[str], keywords: list[str], language: str,
                        limit: int = 40) -> list[ChannelCandidate]:
        """Lives en cours dans les catégories (jeux) du thème, triés par spectateurs,
        puis chaînes correspondant aux mots-clés. API Helix officielle uniquement."""
        found: dict[str, ChannelCandidate] = {}
        lang = language or None
        for name in categories[:3]:
            games = self.helix.get("/search/categories", query=name, first=5).get("data", [])
            exact = [g for g in games if g["name"].casefold() == name.casefold()]
            for game in (exact or games)[:1]:
                streams = self.helix.get("/streams", game_id=game["id"], language=lang, first=50).get("data", [])
                for st in streams:
                    found.setdefault(st["user_id"], ChannelCandidate(
                        platform="twitch", channel=st["user_login"], channel_id=st["user_id"],
                        name=st["user_name"], title=st.get("title", ""), category=st.get("game_name", ""),
                        language=st.get("language", ""), viewers=st.get("viewer_count", 0), live=True))
        for kw in keywords[:3]:
            for ch in self.helix.get("/search/channels", query=kw, first=20).get("data", []):
                if lang and ch.get("broadcaster_language") not in (lang, None, ""):
                    continue
                found.setdefault(ch["id"], ChannelCandidate(
                    platform="twitch", channel=ch["broadcaster_login"], channel_id=ch["id"],
                    name=ch.get("display_name", ch["broadcaster_login"]), title=ch.get("title", ""),
                    category=ch.get("game_name", ""), language=ch.get("broadcaster_language", ""),
                    live=bool(ch.get("is_live"))))
        return sorted(found.values(), key=lambda c: (-c.live, -c.viewers))[:limit]


# -- connexion du compte (Device Code Flow) -------------------------------------

def start_device_login() -> dict:
    """Démarre la connexion : renvoie l'URL et le code à saisir sur twitch.tv/activate."""
    creds = _app_credentials()
    return _post_form(f"{ID}/device", {"client_id": creds["client_id"], "scopes": SCOPES})


def finish_device_login(device: dict, poll: float | None = None, timeout: float = 600) -> str:
    """Attend la validation du code ; enregistre le jeton dans le trousseau.
    Renvoie le pseudo du compte connecté."""
    creds = _app_credentials()
    interval = poll or device.get("interval", 5)
    deadline = time.time() + min(timeout, device.get("expires_in", timeout))
    while time.time() < deadline:
        time.sleep(interval)
        try:
            token = _post_form(f"{ID}/token", {
                "client_id": creds["client_id"], "client_secret": creds["client_secret"],
                "scopes": SCOPES, "device_code": device["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        except urllib.error.HTTPError as exc:
            message = exc.read().decode()
            if "authorization_pending" in message:
                continue
            if "slow_down" in message:
                interval += 5
                continue
            raise PermanentError(f"connexion Twitch refusée : {message[:200]}") from exc
        user = _validate(token["access_token"])
        secrets.set_json(USER_TOKEN_REF, {**token, "user_id": user["user_id"], "login": user["login"],
                                          "obtained_at": time.time()})
        return user["login"]
    raise PermanentError("code Twitch expiré : relance la connexion")


def _validate(access_token: str) -> dict:
    req = urllib.request.Request(f"{ID}/validate", headers={"Authorization": f"OAuth {access_token}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def user_token() -> dict | None:
    """Jeton utilisateur valide (rafraîchi si besoin), ou None si pas de compte connecté."""
    token = secrets.get_json(USER_TOKEN_REF)
    if not token:
        return None
    try:
        _validate(token["access_token"])
        return token
    except urllib.error.HTTPError:
        creds = _app_credentials()
        fresh = _post_form(f"{ID}/token", {"client_id": creds["client_id"],
                                            "client_secret": creds["client_secret"],
                                            "grant_type": "refresh_token",
                                            "refresh_token": token["refresh_token"]})
        token = {**token, **fresh, "obtained_at": time.time()}
        secrets.set_json(USER_TOKEN_REF, token)
        return token


# -- enregistrement du chat en direct (EventSub WebSocket) ----------------------

def store_chat_event(conn: sqlite3.Connection, payload: dict, streams: dict[str, str]) -> bool:
    """Enregistre un message `channel.chat.message`. `streams` : chaîne -> live en cours.
    Seuls l'horodatage et le texte sont gardés (pas de pseudo)."""
    if payload.get("metadata", {}).get("message_type") != "notification":
        return False
    sub = payload["payload"]["subscription"]
    if sub.get("type") != "channel.chat.message":
        return False
    event = payload["payload"]["event"]
    channel_id = event["broadcaster_user_id"]
    stream_id = streams.get(channel_id)
    if stream_id is None:
        return False  # chaîne hors ligne : message hors live ignoré
    ts = _ts(payload["metadata"]["message_timestamp"])
    with tx(conn):
        conn.execute("INSERT INTO chat_message (platform, channel_id, stream_id, ts, text) VALUES ('twitch', ?, ?, ?, ?)",
                     (channel_id, stream_id, ts, event.get("message", {}).get("text", "")[:500]))
    return True


class ChatRecorder:
    """Thread du démon : surveille les chaînes Twitch suivies et enregistre leur
    chat pendant les lives. Se reconnecte tout seul."""

    def __init__(self, db_path: Path, stop: threading.Event, poll_seconds: float = 60) -> None:
        self.db_path = db_path
        self.stop = stop
        self.poll_seconds = poll_seconds
        self.helix = HelixClient()
        self.streams: dict[str, str] = {}  # channel_id -> stream_id en cours
        self.subscribed: set[str] = set()

    def _channels(self, conn: sqlite3.Connection) -> list[str]:
        return [r["channel_id"] for r in conn.execute(
            "SELECT DISTINCT channel_id FROM watch WHERE platform = 'twitch' AND enabled = 1 AND channel_id IS NOT NULL")]

    def _refresh_streams(self, conn: sqlite3.Connection, channels: list[str]) -> None:
        live = {}
        for i in range(0, len(channels), 100):
            for s in self.helix.get("/streams", user_id=channels[i:i + 100], first=100).get("data", []):
                live[s["user_id"]] = (s["id"], _ts(s["started_at"]))
        now = time.time()
        with tx(conn):
            for channel_id, (stream_id, started) in live.items():
                conn.execute("INSERT OR IGNORE INTO chat_session (platform, channel_id, stream_id, started_at) "
                             "VALUES ('twitch', ?, ?, ?)", (channel_id, stream_id, started))
            for channel_id, stream_id in self.streams.items():
                if channel_id not in live:
                    conn.execute("UPDATE chat_session SET ended_at = ? WHERE platform = 'twitch' AND stream_id = ?",
                                 (now, stream_id))
        self.streams = {c: sid for c, (sid, _) in live.items()}

    def _subscribe(self, session_id: str, token: dict, channel_id: str) -> None:
        body = json.dumps({"type": "channel.chat.message", "version": "1",
                           "condition": {"broadcaster_user_id": channel_id, "user_id": token["user_id"]},
                           "transport": {"method": "websocket", "session_id": session_id}}).encode()
        req = urllib.request.Request(f"{HELIX}/eventsub/subscriptions", data=body, method="POST", headers={
            "Client-Id": _app_credentials()["client_id"], "Authorization": f"Bearer {token['access_token']}",
            "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=20).close()
            self.subscribed.add(channel_id)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:  # déjà abonné
                self.subscribed.add(channel_id)
            else:
                log.warning("abonnement EventSub refusé pour %s : %s", channel_id, exc.read().decode()[:200])

    def run(self) -> None:
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect as ws_connect

        from .. import db

        conn = db.connect(self.db_path)
        while not self.stop.is_set():
            try:
                token = user_token()
                channels = self._channels(conn)
                if token is None or not channels:
                    self.stop.wait(self.poll_seconds)
                    continue
                self.subscribed.clear()
                with ws_connect(EVENTSUB_WS, open_timeout=20) as ws:
                    welcome = json.loads(ws.recv(timeout=20))
                    session_id = welcome["payload"]["session"]["id"]
                    keepalive = welcome["payload"]["session"].get("keepalive_timeout_seconds", 10)
                    next_poll = 0.0
                    while not self.stop.is_set():
                        if time.time() >= next_poll:
                            channels = self._channels(conn)
                            self._refresh_streams(conn, channels)
                            for channel_id in channels:
                                if channel_id not in self.subscribed:
                                    self._subscribe(session_id, token, channel_id)
                            next_poll = time.time() + self.poll_seconds
                        try:
                            raw = ws.recv(timeout=keepalive + 5)
                        except TimeoutError:
                            break  # plus de keepalive : on se reconnecte
                        message = json.loads(raw)
                        kind = message.get("metadata", {}).get("message_type")
                        if kind == "session_reconnect":
                            break
                        if kind == "revocation":
                            self.subscribed.discard(message["payload"]["subscription"]["condition"]["broadcaster_user_id"])
                        store_chat_event(conn, message, self.streams)
            except (ConnectionClosed, OSError, urllib.error.URLError) as exc:
                log.warning("chat Twitch : connexion perdue (%s), nouvel essai", exc)
                self.stop.wait(10)
            except PermanentError as exc:
                log.warning("chat Twitch désactivé : %s", exc)
                self.stop.wait(self.poll_seconds * 5)
            except Exception:  # noqa: BLE001 : le thread ne doit jamais mourir
                log.exception("erreur de l'enregistreur de chat Twitch")
                self.stop.wait(30)
        conn.close()
