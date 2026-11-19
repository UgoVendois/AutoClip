"""Secrets (clés d'API, tokens OAuth) dans le trousseau système via `keyring`.

Rien n'est jamais écrit en clair sur disque ni en base : la base ne garde que
le nom de l'entrée (`account.keyring_ref`).
"""

from __future__ import annotations

import json
from typing import Any

SERVICE = "autoclip"

# Champs attendus pour les clés d'application de chaque plateforme.
CREDENTIAL_FIELDS: dict[str, tuple[str, ...]] = {
    "youtube": ("client_id", "client_secret"),
    "tiktok": ("client_key", "client_secret"),
    "instagram": ("app_id", "app_secret"),
    "anthropic": ("api_key",),
    "twitch": ("client_id", "client_secret"),
    "youtube_data": ("api_key",),  # lecture seule : commentaires horodatés
    "gemini": ("api_key",),
    "openai": ("api_key",),
    "mistral": ("api_key",),
}
# Champs facultatifs, gardés s'ils sont remplis.
OPTIONAL_FIELDS: dict[str, tuple[str, ...]] = {
    "instagram": ("config_id",),  # Facebook Login for Business : configuration de connexion
}

_backend: Any = None  # remplaçable dans les tests


def _kr():
    if _backend is not None:
        return _backend
    import keyring

    return keyring


def use_backend(backend: Any) -> None:
    global _backend
    _backend = backend


class MemoryBackend:
    """Trousseau en mémoire, pour les tests et le dry run."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, name: str) -> str | None:
        return self.store.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.store[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        self.store.pop((service, name), None)


def get(name: str) -> str | None:
    return _kr().get_password(SERVICE, name)


def set(name: str, value: str) -> None:  # noqa: A001
    _kr().set_password(SERVICE, name, value)


def delete(name: str) -> None:
    try:
        _kr().delete_password(SERVICE, name)
    except Exception:  # noqa: BLE001 : entrée déjà absente
        pass


def get_json(name: str) -> dict | None:
    raw = get(name)
    return json.loads(raw) if raw else None


def set_json(name: str, data: dict) -> None:
    set(name, json.dumps(data))


# -- clés d'application par plateforme ---------------------------------------

def credentials_key(platform: str) -> str:
    return f"credentials:{platform}"


def set_credentials(platform: str, values: dict[str, str]) -> None:
    fields = CREDENTIAL_FIELDS.get(platform)
    if fields is None:
        raise ValueError(f"plateforme inconnue : {platform}")
    missing = [f for f in fields if not values.get(f)]
    if missing:
        raise ValueError(f"champs manquants pour {platform} : {', '.join(missing)}")
    data = {f: values[f] for f in fields}
    data.update({f: values[f] for f in OPTIONAL_FIELDS.get(platform, ()) if values.get(f)})
    set_json(credentials_key(platform), data)


def get_credentials(platform: str) -> dict | None:
    return get_json(credentials_key(platform))


def credentials_status() -> dict[str, bool]:
    """Indique seulement si une clé est enregistrée, jamais sa valeur."""
    return {p: get(credentials_key(p)) is not None for p in CREDENTIAL_FIELDS}


def youtube_credentials_from_client_secret(data: dict) -> dict[str, str]:
    """Accepte le fichier client_secret.json téléchargé depuis Google Cloud."""
    block = data.get("installed") or data.get("web") or data
    return {"client_id": block["client_id"], "client_secret": block["client_secret"]}
