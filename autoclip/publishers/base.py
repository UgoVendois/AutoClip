"""Interface commune des connecteurs de publication."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..jobs.queue import PermanentError


class PermanentPublishError(PermanentError):
    """Refus définitif (vidéo rejetée, compte déconnecté...) : pas de nouvel essai."""


class RetryablePublishError(Exception):
    """Erreur temporaire (réseau, quota, 5xx) : la file de jobs réessaiera."""


@dataclass(frozen=True)
class Account:
    id: int
    platform: str
    display_name: str
    keyring_ref: str | None


@dataclass(frozen=True)
class PublishRequest:
    video_path: Path
    title: str
    description: str
    # Jeton stable par (clip, compte) : permet de retrouver une vidéo déjà
    # envoyée si le démon a planté juste après l'upload.
    token: str
    tags: list[str] = field(default_factory=list)
    privacy: str = "private"


@dataclass(frozen=True)
class PublishResult:
    external_id: str
    url: str


class Publisher(ABC):
    platform: str

    @abstractmethod
    def find_existing(self, req: PublishRequest, account: Account) -> PublishResult | None:
        """Renvoie la publication si ce `req.token` a déjà été envoyé."""

    @abstractmethod
    def publish(self, req: PublishRequest, account: Account) -> PublishResult:
        """Envoie la vidéo. Lève RetryablePublishError ou PermanentPublishError."""

    def publish_once(self, req: PublishRequest, account: Account) -> PublishResult:
        return self.find_existing(req, account) or self.publish(req, account)
