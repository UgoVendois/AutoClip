"""Gestion de la chaîne elle-même (pas des vidéos) : habillage et playlists.

L'API YouTube ne permet ni de créer une chaîne, ni de changer son nom, ni sa
photo de profil : ces trois points restent manuels (YouTube Studio).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from pathlib import Path

from ..pipeline.cache import Cache
from .base import Account, PermanentPublishError

STUDIO_URL = "https://studio.youtube.com/"  # Personnalisation > Profil : nom et photo


class ChannelManager(ABC):
    @abstractmethod
    def upload_banner(self, account: Account, path: Path) -> str:
        """Envoie l'image et renvoie son URL (à passer à update_branding)."""

    @abstractmethod
    def update_branding(self, account: Account, *, description: str | None = None,
                        keywords: list[str] | None = None, banner_url: str | None = None) -> None:
        """Description, mots-clés et bannière ; le titre de la chaîne n'est jamais modifié."""

    @abstractmethod
    def ensure_playlist(self, account: Account, title: str, description: str) -> str:
        """Identifiant de la playlist de ce titre ; la crée si elle n'existe pas."""

    @abstractmethod
    def add_to_playlist(self, account: Account, playlist_id: str, video_id: str) -> None:
        """Ajoute la vidéo, sans doublon."""


def format_keywords(keywords: list[str], limit: int = 500) -> str:
    """Format YouTube : séparés par des espaces, expressions entre guillemets."""
    out: list[str] = []
    for kw in keywords:
        kw = kw.replace('"', "").strip()
        if not kw:
            continue
        token = f'"{kw}"' if " " in kw else kw
        if len(" ".join([*out, token])) > limit:
            break
        out.append(token)
    return " ".join(out)


class DryRunChannelManager(ChannelManager):
    """Rien n'est envoyé : chaque action est écrite dans le cache (dryrun/channel-*.json)."""

    def __init__(self, cache: Cache) -> None:
        self.cache = cache

    def _path(self, account: Account) -> Path:
        return self.cache.path("dryrun", f"channel-{account.id}.json")

    def state(self, account: Account) -> dict:
        path = self._path(account)
        if path.exists():
            return Cache.read_json(path)
        return {"branding": {}, "playlists": {}, "banners": []}

    def _save(self, account: Account, data: dict) -> None:
        Cache.write_json(self._path(account), data)

    def upload_banner(self, account: Account, path: Path) -> str:
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        data = self.state(account)
        data["banners"].append(str(path))
        self._save(account, data)
        return f"file://{path}"

    def update_branding(self, account: Account, *, description=None, keywords=None, banner_url=None) -> None:
        data = self.state(account)
        if description is not None:
            data["branding"]["description"] = description
        if keywords is not None:
            data["branding"]["keywords"] = format_keywords(keywords)
        if banner_url is not None:
            data["branding"]["banner_url"] = banner_url
        data["branding"]["updated_at"] = time.time()
        self._save(account, data)

    def ensure_playlist(self, account: Account, title: str, description: str) -> str:
        data = self.state(account)
        for pid, pl in data["playlists"].items():
            if pl["title"] == title:
                return pid
        pid = f"dryrun-pl{len(data['playlists']) + 1}"
        data["playlists"][pid] = {"title": title, "description": description, "videos": []}
        self._save(account, data)
        return pid

    def add_to_playlist(self, account: Account, playlist_id: str, video_id: str) -> None:
        data = self.state(account)
        videos = data["playlists"][playlist_id]["videos"]
        if video_id not in videos:
            videos.append(video_id)
        self._save(account, data)


class YouTubeChannelManager(ChannelManager):
    """Demande le droit `youtube` (gestion du compte) en plus de l'envoi de vidéos :
    un compte connecté avant cette version doit être reconnecté une fois."""

    def __init__(self) -> None:
        from .youtube import YouTubePublisher

        self._yt = YouTubePublisher()

    def _service(self, account: Account):
        return self._yt._service(account)

    def _call(self, request):
        from googleapiclient.errors import HttpError

        try:
            return request.execute()
        except HttpError as exc:
            if exc.resp.status == 403 and b"insufficient" in (exc.content or b"").lower():
                raise PermanentPublishError(
                    "YouTube : reconnecte la chaîne (Paramètres > Comptes connectés) "
                    "pour autoriser la gestion de la chaîne") from exc
            raise self._yt._map_error(exc) from exc

    def upload_banner(self, account: Account, path: Path) -> str:
        from googleapiclient.http import MediaFileUpload

        yt = self._service(account)
        media = MediaFileUpload(str(path), mimetype="image/jpeg" if str(path).endswith(".jpg") else "image/png")
        return self._call(yt.channelBanners().insert(media_body=media))["url"]

    def update_branding(self, account: Account, *, description=None, keywords=None, banner_url=None) -> None:
        yt = self._service(account)
        items = self._call(yt.channels().list(part="brandingSettings", mine=True)).get("items", [])
        if not items:
            raise PermanentPublishError(f"{account.display_name} : aucune chaîne sur ce compte")
        channel = items[0]
        branding = channel.get("brandingSettings", {})
        settings = branding.setdefault("channel", {})
        if description is not None:
            settings["description"] = description[:1000]
        if keywords is not None:
            settings["keywords"] = format_keywords(keywords)
        if banner_url is not None:
            branding.setdefault("image", {})["bannerExternalUrl"] = banner_url
        # Le titre doit rester celui de la chaîne : l'API refuse toute modification.
        self._call(yt.channels().update(part="brandingSettings",
                                        body={"id": channel["id"], "brandingSettings": branding}))

    def ensure_playlist(self, account: Account, title: str, description: str) -> str:
        yt = self._service(account)
        page = None
        while True:
            resp = self._call(yt.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=page))
            for item in resp.get("items", []):
                if item["snippet"]["title"] == title:
                    return item["id"]
            page = resp.get("nextPageToken")
            if not page:
                break
        body = {"snippet": {"title": title[:150], "description": description[:5000]},
                "status": {"privacyStatus": "public"}}
        return self._call(yt.playlists().insert(part="snippet,status", body=body))["id"]

    def add_to_playlist(self, account: Account, playlist_id: str, video_id: str) -> None:
        yt = self._service(account)
        existing = self._call(yt.playlistItems().list(part="contentDetails", playlistId=playlist_id,
                                                      videoId=video_id, maxResults=1))
        if existing.get("items"):
            return
        body = {"snippet": {"playlistId": playlist_id,
                            "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
        self._call(yt.playlistItems().insert(part="snippet", body=body))


def channel_manager_for(platform: str) -> ChannelManager:
    if platform == "youtube":
        return YouTubeChannelManager()
    raise PermanentPublishError(f"gestion de chaîne non disponible pour {platform!r}")
