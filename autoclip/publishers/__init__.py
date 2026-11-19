from .base import (Account, PermanentPublishError, Publisher, PublishRequest, PublishResult,
                   RetryablePublishError)

__all__ = ["Account", "PermanentPublishError", "Publisher", "PublishRequest", "PublishResult",
           "RetryablePublishError", "publisher_for_platform", "connect_flow", "CONNECTABLE"]


CONNECTABLE = ("youtube", "tiktok", "instagram")


def publisher_for_platform(platform: str, cache=None, settings: dict | None = None) -> Publisher:
    settings = settings or {}
    if platform == "youtube":
        from .youtube import YouTubePublisher

        return YouTubePublisher()
    if platform in ("tiktok", "instagram"):
        if cache is None:
            raise PermanentPublishError(f"{platform} : cache requis pour suivre les envois")
        if platform == "tiktok":
            from .tiktok import TikTokPublisher

            return TikTokPublisher(cache, privacy=str(settings.get("tiktok_privacy", "SELF_ONLY")))
        from .instagram import InstagramPublisher

        return InstagramPublisher(cache)
    raise PermanentPublishError(f"aucun connecteur pour la plateforme {platform!r}")


def connect_flow(platform: str):
    """Connexion OAuth d'un compte depuis l'interface : objet avec auth_url, deadline,
    cancel() et wait_and_save(conn) -> id du compte."""
    if platform == "youtube":
        from .youtube import YouTubeConnect

        return YouTubeConnect()
    if platform == "tiktok":
        from .tiktok import TikTokConnect

        return TikTokConnect()
    if platform == "instagram":
        from .instagram import InstagramConnect

        return InstagramConnect()
    raise ValueError(f"connexion impossible pour {platform!r}")
