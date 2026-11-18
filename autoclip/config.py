"""Emplacements des données (base SQLite, cache) et réglages par défaut."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def default_home() -> Path:
    if env := os.environ.get("AUTOCLIP_HOME"):
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(xdg).expanduser() / "autoclip"


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def db(self) -> Path:
        return self.home / "autoclip.db"

    @property
    def cache(self) -> Path:
        return self.home / "cache"

    def ensure(self) -> "Paths":
        self.cache.mkdir(parents=True, exist_ok=True)
        return self


# Réglages globaux (table `setting`), modifiables depuis l'API.
DEFAULT_SETTINGS: dict[str, object] = {
    "whisper_model": "small",
    "whisper_language": None,  # None = détection automatique
    "youtube_privacy": "private",  # private | unlisted | public
    "default_dry_run": True,
}

# Paramètres de clipping propres à chaque bot (colonne `bot.clip_settings`).
DEFAULT_CLIP_SETTINGS: dict[str, object] = {
    "min_seconds": 20,
    "max_seconds": 60,
    "max_clips": 5,
    "subtitle_words": 3,  # mots affichés ensemble, le mot prononcé est surligné
    "font_size": 84,
    "font": "DejaVu Sans",
    "tags": [],
}

API_HOST = "127.0.0.1"
API_PORT = 8765
