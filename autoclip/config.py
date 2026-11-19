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
    "ui_theme": "system",  # system | light | dark
    # Agent identité de chaîne (textes, puis visuels).
    "identity_provider": "anthropic",  # anthropic | gemini | openai | mistral | ollama | fake
    "identity_model": None,  # None = modèle par défaut du fournisseur
    "image_provider": "svg",  # svg (dessiné par le modèle de texte) | gemini | openai | fake
    "image_model": None,
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
    # Mise en page 9:16 : auto (facecam en haut si un visage fixe est détecté, sinon
    # jeu entier sur fond flouté) | facecam | fit (fond flouté) | crop (recadrage central).
    "layout": "auto",
    "trim_silences": True,  # coupe les silences (ni parole ni son) de plus de 0,7 s
    "hook_text": True,  # texte d'accroche incrusté au début du clip
    "hook_seconds": 3.0,
    "auto_approve": False,  # valider chaque clip dès la fin de son montage (sans relecture)
}

# Découverte automatique et IA, propres à chaque bot (colonne `bot.ai_settings`).
DEFAULT_AI_SETTINGS: dict[str, object] = {
    "provider": "none",  # none | anthropic | gemini | openai | mistral | ollama | fake
    "model": None,  # None = modèle par défaut du fournisseur
    "effort": "low",  # anthropic : low | medium | high
    "daily_token_budget": 200_000,  # entrée + sortie, tous appels du jour
    "max_windows": 8,  # fenêtres présélectionnées par vidéo
    "density_seconds": 30,  # fenêtre glissante de densité du chat
    "margin_seconds": 8,  # marge autour d'un pic
    "min_score": 60,  # score IA minimal (0-100) pour proposer un clip
    "clips_per_video": 3,
    "backfill_hours": 48,  # à l'ajout d'une chaîne : contenus récents à reprendre
    "vision": True,  # images clés envoyées à l'IA (action à l'écran, pas seulement la parole)
    "chat_delay_seconds": 8,  # retard du chat sur l'action (latence du live + temps de réaction)
    # Éclaireur : l'IA trouve elle-même les chaînes à surveiller à partir d'un thème.
    "scout_enabled": False,
    "scout_theme": "",  # ex. « GTA 6, streamers français, moments drôles »
    "scout_platforms": ["twitch", "youtube"],
    "scout_language": "fr",  # langue des streams (code ISO), vide = toutes
    "scout_max_channels": 5,  # chaînes trouvées par l'IA surveillées en même temps
    "scout_every_hours": 24,
}

API_HOST = "127.0.0.1"
API_PORT = 8765
