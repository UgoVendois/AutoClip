"""Interface des découvreurs de contenu et registre des plateformes."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..jobs.queue import PermanentError


@dataclass(frozen=True)
class DiscoveredItem:
    external_id: str
    url: str
    title: str
    published_at: float
    duration: float | None = None
    channel_id: str | None = None
    stream_id: str | None = None  # Twitch : identifiant du live d'origine
    live: bool = False  # encore en cours : à reprendre plus tard


@dataclass(frozen=True)
class ChannelCandidate:
    """Chaîne trouvée par une recherche (éclaireur IA)."""

    platform: str
    channel: str  # pseudo (Twitch) ou identifiant UC... (YouTube) : ce qui sera surveillé
    channel_id: str
    name: str
    title: str = ""  # titre du live ou de la vidéo trouvée
    category: str = ""
    language: str = ""
    viewers: int = 0  # spectateurs en direct (Twitch) ou vues de la vidéo trouvée (YouTube)
    live: bool = False

    def brief(self) -> dict:
        return {"platform": self.platform, "channel_id": self.channel_id, "name": self.name,
                "title": self.title[:120], "category": self.category, "language": self.language,
                "viewers": self.viewers, "live": self.live}


@dataclass
class Signal:
    kind: str  # chat | comments | audio
    events: list[float] = field(default_factory=list)  # secondes depuis le début de la vidéo


class SourceDiscoverer(ABC):
    """Trouve les nouveaux contenus d'une chaîne et les signaux bon marché associés.

    Pour ajouter une plateforme : sous-classer, puis décorer avec @register."""

    platform: str

    @abstractmethod
    def resolve_channel(self, channel: str) -> str:
        """Pseudo, URL ou identifiant -> identifiant stable de la chaîne."""

    @abstractmethod
    def list_recent(self, channel_id: str, since: float) -> list[DiscoveredItem]:
        """Contenus publiés après `since` (timestamp Unix)."""

    def signal(self, conn: sqlite3.Connection, item: DiscoveredItem) -> Signal | None:
        """Événements horodatés (chat, commentaires) ; None = rien d'exploitable."""
        return None

    def search_channels(self, *, categories: list[str], keywords: list[str], language: str,
                        limit: int = 40) -> list[ChannelCandidate]:
        """Chaînes actives sur un thème (éclaireur). Par défaut : non pris en charge."""
        return []

    def download_section(self, item: DiscoveredItem, start: float, end: float, dest: Path) -> Path:
        """Télécharge uniquement [start, end] de la vidéo."""
        from ..pipeline.ingest import download_section

        return download_section(item.url, start, end, dest)

    def download_audio(self, item: DiscoveredItem, dest: Path, should_stop=None) -> Path:
        """Piste audio seule, pour le signal d'énergie quand il n'y a pas de chat."""
        from ..pipeline.ingest import download_audio

        return download_audio(item.url, dest, should_stop)


REGISTRY: dict[str, type[SourceDiscoverer]] = {}
_instances: dict[str, SourceDiscoverer] = {}


def register(cls: type[SourceDiscoverer]) -> type[SourceDiscoverer]:
    REGISTRY[cls.platform] = cls
    return cls


def get_discoverer(platform: str) -> SourceDiscoverer:
    if platform not in _instances:
        if platform not in REGISTRY:
            raise PermanentError(f"plateforme non prise en charge : {platform!r} "
                                 f"(disponibles : {', '.join(sorted(REGISTRY))})")
        _instances[platform] = REGISTRY[platform]()
    return _instances[platform]


def use_discoverer(platform: str, instance: SourceDiscoverer | None) -> None:
    """Remplace l'instance d'une plateforme (tests)."""
    if instance is None:
        _instances.pop(platform, None)
    else:
        _instances[platform] = instance
