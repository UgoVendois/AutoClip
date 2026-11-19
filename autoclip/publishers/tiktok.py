"""Connecteur TikTok : connexion (Login Kit, appli de bureau) et publication
directe (Content Posting API, envoi du fichier par morceaux).

Tant que l'app TikTok n'est pas auditée, TikTok n'accepte que des publications
privées, sur un compte privé. La confidentialité choisie (réglage
`tiktok_privacy`) est donc ramenée à une option permise par le compte.
"""

from __future__ import annotations

import sqlite3
import time
import urllib.parse
from pathlib import Path

from .. import secrets
from ..pipeline.cache import Cache
from .base import (Account, PermanentPublishError, Publisher, PublishRequest, PublishResult,
                   RetryablePublishError)
from .http import HTTPStatusError, request
from .oauth import LoopbackFlow, pkce_pair, save_account

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,user.info.profile,video.publish"
PRIVACY_LEVELS = ("SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "PUBLIC_TO_EVERYONE")

MB = 1_000_000
SINGLE_UPLOAD_MAX = 64 * MB  # au-delà, envoi obligatoire en plusieurs morceaux
CHUNK = 10 * MB  # entre 5 et 64 Mo ; le dernier morceau absorbe le reste
POLL_SECONDS = 5
POLL_TIMEOUT = 600
UPLOAD_URL_TTL = 3600  # l'adresse d'envoi expire au bout d'une heure

HELP = ("Vérifie dans developers.tiktok.com : produits Login Kit (Desktop) et Content Posting API "
        "(Direct Post activé), scopes user.info.basic, user.info.profile et video.publish, et l'adresse "
        "de redirection http://127.0.0.1:*/callback/ (onglet Desktop). En mode Sandbox, ajoute ton "
        "compte TikTok comme utilisateur de test.")
UNAUDITED_HELP = ("TikTok : app non auditée, publication possible uniquement sur un compte privé. "
                  "Passe ton compte TikTok en privé (Paramètres > Confidentialité), ou fais auditer "
                  "l'app (Content Posting API) pour publier en public.")


def _credentials() -> dict:
    creds = secrets.get_credentials("tiktok")
    if not creds:
        raise PermanentPublishError("clés TikTok absentes : Paramètres > Clés d'API > TikTok")
    return creds


def _error(data: dict) -> tuple[str, str]:
    err = data.get("error") or {}
    if isinstance(err, str):  # endpoint OAuth : {"error": "...", "error_description": "..."}
        return err, data.get("error_description", "")
    return err.get("code", ""), err.get("message", "")


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) selon les règles de TikTok."""
    if size <= SINGLE_UPLOAD_MAX:
        return size, 1
    return CHUNK, size // CHUNK


def chunk_ranges(size: int) -> list[tuple[int, int]]:
    """Intervalles [début, fin] (inclus) de chaque morceau ; le dernier prend le reste."""
    chunk_size, count = chunk_plan(size)
    ranges = [(i * chunk_size, (i + 1) * chunk_size - 1) for i in range(count)]
    ranges[-1] = (ranges[-1][0], size - 1)
    return ranges


# -- jetons ---------------------------------------------------------------------------

def _store(ref: str, tokens: dict, now: float) -> dict:
    data = {"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"],
            "open_id": tokens.get("open_id"), "scope": tokens.get("scope", ""),
            "expires_at": now + float(tokens.get("expires_in", 7200))}
    secrets.set_json(ref, data)
    return data


def access_token(account: Account) -> str:
    """Jeton valide ; renouvelé (et réenregistré : TikTok le fait tourner) s'il expire."""
    if not account.keyring_ref:
        raise PermanentPublishError("compte TikTok sans jeton : reconnecte-le dans Paramètres")
    data = secrets.get_json(account.keyring_ref)
    if not data:
        raise PermanentPublishError("jeton TikTok introuvable : reconnecte le compte dans Paramètres")
    if data.get("expires_at", 0) - 120 > time.time():
        return data["access_token"]
    return refresh(account.keyring_ref, data)["access_token"]


def refresh(ref: str, data: dict) -> dict:
    creds = _credentials()
    try:
        resp = request("POST", f"{API}/oauth/token/", form={
            "client_key": creds["client_key"], "client_secret": creds["client_secret"],
            "grant_type": "refresh_token", "refresh_token": data["refresh_token"]})
    except HTTPStatusError as exc:
        raise PermanentPublishError(f"TikTok : reconnexion nécessaire ({_error(exc.data)}).") from exc
    if "access_token" not in resp.data:
        raise PermanentPublishError(f"TikTok : reconnexion nécessaire ({_error(resp.data)}).")
    return _store(ref, resp.data, time.time())


def profile_url(username: str | None) -> str | None:
    return f"https://www.tiktok.com/@{username}" if username else None


# -- connexion ----------------------------------------------------------------------------

class TikTokConnect(LoopbackFlow):
    platform = "tiktok"
    label = "TikTok"

    def authorization_url(self) -> str:
        self.verifier, challenge = pkce_pair(hex_challenge=True)  # TikTok bureau : SHA-256 en hexa
        return AUTH_URL + "?" + urllib.parse.urlencode({
            "client_key": _credentials()["client_key"], "scope": SCOPES, "response_type": "code",
            "redirect_uri": self.redirect_uri, "state": self.state,
            "code_challenge": challenge, "code_challenge_method": "S256"})

    def help(self) -> str:
        return HELP

    def exchange(self, code: str) -> dict:
        creds = _credentials()
        try:
            resp = request("POST", f"{API}/oauth/token/", form={
                "client_key": creds["client_key"], "client_secret": creds["client_secret"], "code": code,
                "grant_type": "authorization_code", "redirect_uri": self.redirect_uri,
                "code_verifier": self.verifier})
        except HTTPStatusError as exc:
            raise PermanentPublishError(f"TikTok : échange du code refusé ({_error(exc.data)}). {HELP}") from exc
        if "access_token" not in resp.data:
            raise PermanentPublishError(f"TikTok : échange du code refusé ({_error(resp.data)}). {HELP}")
        return resp.data

    def save(self, conn: sqlite3.Connection, tokens: dict) -> int:
        ref = f"token:tiktok:{tokens['open_id']}"
        _store(ref, tokens, time.time())
        user = {}
        try:
            resp = request("GET", f"{API}/user/info/",
                           params={"fields": "open_id,display_name,username,profile_deep_link"},
                           headers={"Authorization": f"Bearer {tokens['access_token']}"})
            user = resp.data.get("data", {}).get("user", {})
        except (HTTPStatusError, RetryablePublishError):
            pass  # le nom affiché est un confort : la connexion reste valable
        username = user.get("username")
        name = user.get("display_name") or (f"@{username}" if username else "Compte TikTok")
        if username and name != f"@{username}":
            name = f"{name} (@{username})"
        return save_account(conn, "tiktok", ref, name, profile_url(username))


# -- publication ----------------------------------------------------------------------------

class TikTokPublisher(Publisher):
    platform = "tiktok"

    def __init__(self, cache: Cache, *, privacy: str = "SELF_ONLY", sleep=time.sleep,
                 clock=time.time) -> None:
        self.cache = cache
        self.privacy = privacy
        self.sleep = sleep
        self.clock = clock

    # État local (publish_id) : retrouver un envoi déjà fait si le démon s'est arrêté.
    def _state_path(self, req: PublishRequest) -> Path:
        return self.cache.path("publish", f"tiktok-{req.token}.json")

    def _state(self, req: PublishRequest) -> dict | None:
        path = self._state_path(req)
        return Cache.read_json(path) if path.exists() else None

    def _api(self, account: Account, path: str, body: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {access_token(account)}"}
        try:
            resp = request("POST", f"{API}{path}", json_body=body or {}, headers=headers)
        except HTTPStatusError as exc:
            code, message = _error(exc.data)
            if code == "access_token_invalid" or exc.status == 401:
                raise PermanentPublishError("TikTok : autorisation expirée, reconnecte le compte "
                                            "dans Paramètres > Comptes connectés.") from exc
            if code == "unaudited_client_can_only_post_to_private_accounts":
                raise PermanentPublishError(UNAUDITED_HELP) from exc
            if code in ("spam_risk_too_many_posts", "reached_active_user_cap", "rate_limit_exceeded"):
                raise RetryablePublishError(f"TikTok : quota de publication atteint ({code}), nouvel essai plus tard") from exc
            raise PermanentPublishError(f"TikTok : {code or exc.status} {message}".strip()) from exc
        code, message = _error(resp.data)
        if code not in ("", "ok"):
            raise PermanentPublishError(f"TikTok : {code} {message}".strip())
        return resp.data.get("data", {})

    def _privacy(self, options: list[str]) -> str:
        if self.privacy in options:
            return self.privacy
        return "SELF_ONLY" if "SELF_ONLY" in options or not options else options[0]

    def _result(self, account: Account, req: PublishRequest, status: dict, publish_id: str,
                creator: str | None = None) -> PublishResult:
        posts = status.get("publicaly_available_post_id") or []
        username = creator or (self._state(req) or {}).get("creator")
        if posts and username:
            return PublishResult(str(posts[0]), f"https://www.tiktok.com/@{username}/video/{posts[0]}")
        # Publication privée : pas d'identifiant public, lien vers le profil.
        return PublishResult(publish_id, profile_url(username) or "https://www.tiktok.com/")

    def _wait(self, account: Account, req: PublishRequest, publish_id: str) -> PublishResult:
        deadline = self.clock() + POLL_TIMEOUT
        while True:
            status = self._api(account, "/post/publish/status/fetch/", {"publish_id": publish_id})
            state = status.get("status")
            if state in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                return self._result(account, req, status, publish_id)
            if state == "FAILED":
                reason = status.get("fail_reason", "")
                self._state_path(req).unlink(missing_ok=True)
                if reason in ("internal", "video_pull_failed"):
                    raise RetryablePublishError(f"TikTok : échec temporaire ({reason})")
                if reason == "auth_removed":
                    raise PermanentPublishError("TikTok : autorisation retirée, reconnecte le compte.")
                raise PermanentPublishError(f"TikTok a refusé la vidéo ({reason or 'raison inconnue'})")
            if self.clock() > deadline:
                raise RetryablePublishError("TikTok traite encore la vidéo : nouvelle vérification plus tard")
            self.sleep(POLL_SECONDS)

    def find_existing(self, req: PublishRequest, account: Account) -> PublishResult | None:
        state = self._state(req)
        if not state:
            return None
        if not state.get("uploaded") and self.clock() - state.get("created_at", 0) > UPLOAD_URL_TTL:
            self._state_path(req).unlink(missing_ok=True)  # envoi interrompu, adresse expirée : on refait
            return None
        if not state.get("uploaded"):
            return None  # l'envoi est repris par publish()
        return self._wait(account, req, state["publish_id"])

    def publish(self, req: PublishRequest, account: Account) -> PublishResult:
        size = req.video_path.stat().st_size
        state = self._state(req)
        if not state:
            creator = self._api(account, "/post/publish/creator_info/query/")
            privacy = self._privacy(creator.get("privacy_level_options") or [])
            chunk_size, count = chunk_plan(size)
            init = self._api(account, "/post/publish/video/init/", {
                "post_info": {"title": caption(req), "privacy_level": privacy, "disable_duet": False,
                              "disable_comment": False, "disable_stitch": False,
                              "video_cover_timestamp_ms": 1000},
                "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size,
                                "total_chunk_count": count},
            })
            state = {"publish_id": init["publish_id"], "upload_url": init["upload_url"],
                     "creator": creator.get("creator_username"), "created_at": self.clock(),
                     "uploaded": False}
            Cache.write_json(self._state_path(req), state)
        self._upload(req.video_path, size, state["upload_url"])
        state["uploaded"] = True
        Cache.write_json(self._state_path(req), state)
        return self._wait(account, req, state["publish_id"])

    def _upload(self, path: Path, size: int, url: str) -> None:
        with path.open("rb") as f:
            for first, last in chunk_ranges(size):
                f.seek(first)
                part = f.read(last - first + 1)
                try:
                    resp = request("PUT", url, data=part, timeout=600, headers={
                        "Content-Type": "video/mp4", "Content-Length": str(len(part)),
                        "Content-Range": f"bytes {first}-{last}/{size}"})
                except HTTPStatusError as exc:
                    if exc.status in (403, 404):  # adresse expirée ou envoi inconnu : on recommence
                        raise RetryablePublishError("TikTok : adresse d'envoi expirée, nouvel essai") from exc
                    raise PermanentPublishError(f"TikTok : envoi refusé (HTTP {exc.status})") from exc
                if resp.status not in (200, 201, 206):
                    raise RetryablePublishError(f"TikTok : envoi inattendu (HTTP {resp.status})")


def caption(req: PublishRequest) -> str:
    """Légende TikTok : titre, description, hashtags (2 200 caractères au plus)."""
    tags = " ".join(f"#{t.lstrip('#').replace(' ', '')}" for t in req.tags if t.strip())
    parts = [req.title.strip(), req.description.strip(), tags]
    return "\n\n".join(p for p in parts if p)[:2200]

