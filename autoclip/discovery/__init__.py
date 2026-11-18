"""Découverte automatique de contenu. Importer ce paquet enregistre les plateformes."""

from . import twitch, youtube  # noqa: F401  (enregistrement dans REGISTRY)
from .base import (REGISTRY, DiscoveredItem, Signal, SourceDiscoverer, get_discoverer, register,
                   use_discoverer)

__all__ = ["REGISTRY", "DiscoveredItem", "Signal", "SourceDiscoverer", "get_discoverer", "register",
           "use_discoverer"]
