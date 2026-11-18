"""Connecteur factice : tout le circuit s'exécute, rien n'est publié.

Chaque « publication » est écrite dans un fichier JSON du cache, ce qui permet
de vérifier ce qui aurait été envoyé (titre, description, fichier...).
"""

from __future__ import annotations

import time
from pathlib import Path

from ..pipeline.cache import Cache
from .base import Account, Publisher, PublishRequest, PublishResult


class DryRunPublisher(Publisher):
    platform = "dry-run"

    def __init__(self, cache: Cache) -> None:
        self.cache = cache

    def _record(self, token: str) -> Path:
        return self.cache.path("dryrun", f"{token}.json")

    def find_existing(self, req: PublishRequest, account: Account) -> PublishResult | None:
        path = self._record(req.token)
        if path.exists():
            data = Cache.read_json(path)
            return PublishResult(data["external_id"], data["url"])
        return None

    def publish(self, req: PublishRequest, account: Account) -> PublishResult:
        if not Path(req.video_path).is_file():
            raise FileNotFoundError(req.video_path)
        result = PublishResult(external_id=f"dryrun-{req.token}", url=f"file://{req.video_path}")
        Cache.write_json(self._record(req.token), {
            "external_id": result.external_id,
            "url": result.url,
            "platform": account.platform,
            "account": account.display_name,
            "title": req.title,
            "description": req.description,
            "tags": req.tags,
            "privacy": req.privacy,
            "video": str(req.video_path),
            "published_at": time.time(),
        })
        return result
