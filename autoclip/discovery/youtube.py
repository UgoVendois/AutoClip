"""Découverte YouTube.

- Nouvelles vidéos : flux RSS officiel de la chaîne (gratuit, sans quota).
- Signal : horodatages cités dans les commentaires (YouTube Data API v3,
  `commentThreads.list`, clé `youtube_data` facultative). Sans clé, ou s'il y
  en a trop peu, le pipeline se rabat sur l'énergie audio.
- Le chat des lives rediffusés n'est pas accessible par l'API officielle.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import sqlite3
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

from .. import secrets
from ..jobs.queue import PermanentError
from ..signals.peaks import comment_timestamps
from .base import ChannelCandidate, DiscoveredItem, Signal, SourceDiscoverer, register

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
API = "https://www.googleapis.com/youtube/v3"
NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")
MIN_TIMESTAMPED_COMMENTS = 8


def _get(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "autoclip/0.3"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


@register
class YouTubeDiscoverer(SourceDiscoverer):
    platform = "youtube"

    def resolve_channel(self, channel: str) -> str:
        channel = channel.strip()
        if CHANNEL_ID.match(channel):
            return channel
        match = re.search(r"/channel/(UC[\w-]{22})", channel)
        if match:
            return match.group(1)
        # @pseudo ou URL de chaîne : on lit l'identifiant via yt-dlp (sans téléchargement).
        import yt_dlp

        url = channel if channel.startswith("http") else f"https://www.youtube.com/{channel if channel.startswith('@') else '@' + channel}"
        with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "playlist_items": "0"}) as ydl:
            info = ydl.extract_info(url, download=False)
        channel_id = info.get("channel_id") or info.get("id")
        if not channel_id or not CHANNEL_ID.match(channel_id):
            raise PermanentError(f"chaîne YouTube introuvable : {channel}")
        return channel_id

    def list_recent(self, channel_id: str, since: float) -> list[DiscoveredItem]:
        root = ET.fromstring(_get(FEED.format(channel_id)))
        items = []
        for entry in root.findall("atom:entry", NS):
            video_id = entry.findtext("yt:videoId", namespaces=NS)
            published = datetime.fromisoformat(entry.findtext("atom:published", namespaces=NS)).timestamp()
            if video_id and published > since:
                items.append(DiscoveredItem(
                    external_id=video_id,
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    title=entry.findtext("atom:title", default="", namespaces=NS),
                    published_at=published,
                    channel_id=channel_id,
                ))
        return items

    def signal(self, conn: sqlite3.Connection, item: DiscoveredItem) -> Signal | None:
        creds = secrets.get_json(secrets.credentials_key("youtube_data"))
        if not creds:
            return None
        texts: list[str] = []
        token = None
        for _ in range(5):  # 5 pages de 100 commentaires = 5 unités de quota
            params = {"part": "snippet", "videoId": item.external_id, "maxResults": 100,
                      "order": "relevance", "textFormat": "plainText", "key": creds["api_key"]}
            if token:
                params["pageToken"] = token
            try:
                data = json.loads(_get(f"{API}/commentThreads?{urllib.parse.urlencode(params)}"))
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 404):  # commentaires désactivés, quota épuisé...
                    return None
                raise
            for thread in data.get("items", []):
                texts.append(thread["snippet"]["topLevelComment"]["snippet"]["textDisplay"])
            token = data.get("nextPageToken")
            if not token:
                break
        events = comment_timestamps(texts, item.duration)
        return Signal("comments", events) if len(events) >= MIN_TIMESTAMPED_COMMENTS else None

    def search_channels(self, *, categories: list[str], keywords: list[str], language: str,
                        limit: int = 40) -> list[ChannelCandidate]:
        """Vidéos récentes les plus vues sur le thème -> leurs chaînes (YouTube Data API,
        `search.list` : 100 unités de quota par recherche, donc 2 recherches au plus)."""
        creds = secrets.get_json(secrets.credentials_key("youtube_data"))
        if not creds:
            return []  # sans clé de lecture : pas de recherche YouTube
        found: dict[str, ChannelCandidate] = {}
        after = datetime.fromtimestamp(time.time() - 14 * 86400).astimezone().isoformat(timespec="seconds")
        for query in [*keywords, *categories][:2]:
            params = {"part": "snippet", "q": query, "type": "video", "order": "viewCount", "maxResults": 25,
                      "publishedAfter": after, "key": creds["api_key"]}
            if language:
                params["relevanceLanguage"] = language
            try:
                data = json.loads(_get(f"{API}/search?{urllib.parse.urlencode(params)}"))
            except urllib.error.HTTPError as exc:
                if exc.code == 403:  # quota épuisé ou clé refusée : on garde le reste
                    break
                raise
            for item in data.get("items", []):
                sn = item["snippet"]
                found.setdefault(sn["channelId"], ChannelCandidate(
                    platform="youtube", channel=sn["channelId"], channel_id=sn["channelId"],
                    name=sn.get("channelTitle", ""), title=sn.get("title", ""), category=query,
                    language=language, live=sn.get("liveBroadcastContent") == "live"))
        return list(found.values())[:limit]
