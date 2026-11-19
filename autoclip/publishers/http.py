"""Requêtes HTTP des connecteurs TikTok et Instagram (bibliothèque standard).

Les erreurs sont classées pour la file de jobs : réseau, 429 et 5xx sont
temporaires (nouvel essai) ; le reste remonte au connecteur, qui décide.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .base import RetryablePublishError

TIMEOUT = 120


@dataclass
class Response:
    status: int
    data: dict
    headers: dict[str, str] = field(default_factory=dict)


class HTTPStatusError(Exception):
    """Réponse 4xx : le connecteur lit `data` pour décider (reconnexion, refus...)."""

    def __init__(self, status: int, data: dict, text: str) -> None:
        super().__init__(f"HTTP {status} : {text[:300]}")
        self.status = status
        self.data = data


def request(method: str, url: str, *, params: dict | None = None, json_body: dict | None = None,
            form: dict | None = None, data: bytes | None = None, headers: dict[str, str] | None = None,
            timeout: float = TIMEOUT) -> Response:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = dict(headers or {})
    body = data
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers.setdefault("Content-Type", "application/json; charset=UTF-8")
    elif form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return Response(resp.status, _json(raw), {k.lower(): v for k, v in resp.headers.items()})
    except urllib.error.HTTPError as exc:
        with exc:
            raw = exc.read()
        text = raw.decode(errors="replace")
        if exc.code == 429 or exc.code >= 500:
            raise RetryablePublishError(f"HTTP {exc.code} : {text[:300]}") from exc
        raise HTTPStatusError(exc.code, _json(raw), text) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise RetryablePublishError(f"réseau : {getattr(exc, 'reason', exc)}") from exc


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        return {"raw": raw[:500].decode(errors="replace")}
    return value if isinstance(value, dict) else {"data": value}
