from .base import (Account, PermanentPublishError, Publisher, PublishRequest, PublishResult,
                   RetryablePublishError)

__all__ = ["Account", "PermanentPublishError", "Publisher", "PublishRequest", "PublishResult",
           "RetryablePublishError", "publisher_for_platform"]


def publisher_for_platform(platform: str) -> Publisher:
    if platform == "youtube":
        from .youtube import YouTubePublisher

        return YouTubePublisher()
    raise PermanentPublishError(f"aucun connecteur pour la plateforme {platform!r} en phase 1")
