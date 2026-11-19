"""Connecteur Instagram (Reels) : Facebook Login for Business et Graph API.

Seule cette connexion permet d'envoyer un fichier depuis l'ordinateur (envoi
« resumable » vers rupload.facebook.com). Il faut un compte Instagram
professionnel (Créateur ou Entreprise) relié à une Page Facebook.

Publication : conteneur REELS -> envoi du fichier -> attente du traitement ->
media_publish -> lien permanent.
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
from .oauth import LoopbackFlow, save_account

VERSION = "v27.0"
GRAPH = f"https://graph.facebook.com/{VERSION}"
DIALOG = f"https://www.facebook.com/{VERSION}/dialog/oauth"
UPLOAD = f"https://rupload.facebook.com/ig-api-upload/{VERSION}"
SCOPES = "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement,business_management"
CONNECT_PORT = 8767  # adresse de retour fixe, à déclarer dans l'app Meta
MAX_BYTES = 300 * 1024 * 1024
POLL_SECONDS = 10
POLL_TIMEOUT = 900
# Erreurs Graph temporaires : limites d'appels, indisponibilité.
TRANSIENT_CODES = {1, 2, 4, 17, 32, 341, 613}

HELP = ("Vérifie dans developers.facebook.com : app de type Entreprise avec les produits « Facebook Login "
        "for Business » et « Instagram », l'adresse http://localhost:8767/callback/ dans « URI de "
        "redirection OAuth valides », et ton compte en administrateur ou testeur de l'app. Ton compte "
        "Instagram doit être professionnel (Créateur ou Entreprise) et relié à une Page Facebook.")


def _credentials() -> dict:
    creds = secrets.get_credentials("instagram")
    if not creds:
        raise PermanentPublishError("clés Meta absentes : Paramètres > Clés d'API > Instagram")
    return creds


def _graph_error(data: dict) -> dict:
    err = data.get("error")
    return err if isinstance(err, dict) else {"message": str(err or data)[:300]}


def _graph(method: str, path: str, **kwargs):
    """Appel Graph ; erreurs Meta traduites pour la file de jobs."""
    url = path if path.startswith("http") else f"{GRAPH}{path}"
    try:
        return request(method, url, **kwargs).data
    except HTTPStatusError as exc:
        err = _graph_error(exc.data)
        code, message = err.get("code"), err.get("error_user_msg") or err.get("message", "")
        if code == 190:
            raise PermanentPublishError("Instagram : autorisation expirée ou retirée, reconnecte le compte "
                                        "dans Paramètres > Comptes connectés.") from exc
        if code in TRANSIENT_CODES or err.get("is_transient"):
            raise RetryablePublishError(f"Instagram : limite temporaire ({message})") from exc
        raise PermanentPublishError(f"Instagram : {message or exc}") from exc


def profile_url(username: str | None) -> str | None:
    return f"https://www.instagram.com/{username}/" if username else None


# -- connexion ------------------------------------------------------------------------------

class InstagramConnect(LoopbackFlow):
    platform = "instagram"
    label = "Instagram"
    host = "localhost"
    port = CONNECT_PORT

    def authorization_url(self) -> str:
        creds = _credentials()
        params = {"client_id": creds["app_id"], "redirect_uri": self.redirect_uri, "state": self.state,
                  "response_type": "code"}
        if creds.get("config_id"):  # configuration Facebook Login for Business
            params["config_id"] = creds["config_id"]
        else:
            params["scope"] = SCOPES
        return DIALOG + "?" + urllib.parse.urlencode(params)

    def help(self) -> str:
        return HELP

    def exchange(self, code: str) -> dict:
        creds = _credentials()
        try:
            short = _graph("GET", "/oauth/access_token", params={
                "client_id": creds["app_id"], "client_secret": creds["app_secret"],
                "redirect_uri": self.redirect_uri, "code": code})
            # Jeton longue durée (~60 jours) : les jetons de Page qui en découlent n'expirent pas.
            long = _graph("GET", "/oauth/access_token", params={
                "grant_type": "fb_exchange_token", "client_id": creds["app_id"],
                "client_secret": creds["app_secret"], "fb_exchange_token": short["access_token"]})
        except PermanentPublishError as exc:
            raise PermanentPublishError(f"{exc} {HELP}") from exc
        return long

    def save(self, conn: sqlite3.Connection, tokens: dict) -> int:
        pages = _graph("GET", "/me/accounts", params={
            "fields": "name,access_token,instagram_business_account{id,username,name}",
            "limit": 100, "access_token": tokens["access_token"]}).get("data", [])
        linked = [p for p in pages if p.get("instagram_business_account")]
        if not linked:
            raise PermanentPublishError(
                "Aucun compte Instagram professionnel trouvé sur tes Pages Facebook. Passe ton compte "
                "Instagram en compte professionnel (Créateur ou Entreprise), relie-le à une Page Facebook, "
                "puis autorise cette Page pendant la connexion. " + HELP)
        first = None
        for page in linked:
            ig = page["instagram_business_account"]
            ref = f"token:instagram:{ig['id']}"
            secrets.set_json(ref, {"page_token": page["access_token"], "page_id": page["id"],
                                   "ig_user_id": ig["id"], "username": ig.get("username")})
            name = f"@{ig['username']}" if ig.get("username") else (ig.get("name") or page.get("name"))
            account_id = save_account(conn, "instagram", ref, name, profile_url(ig.get("username")))
            first = first or account_id
        return first


# -- publication -------------------------------------------------------------------------------

class InstagramPublisher(Publisher):
    platform = "instagram"

    def __init__(self, cache: Cache, *, sleep=time.sleep, clock=time.time) -> None:
        self.cache = cache
        self.sleep = sleep
        self.clock = clock

    def _auth(self, account: Account) -> dict:
        data = secrets.get_json(account.keyring_ref) if account.keyring_ref else None
        if not data:
            raise PermanentPublishError("jeton Instagram introuvable : reconnecte le compte dans Paramètres")
        return data

    def _state_path(self, req: PublishRequest) -> Path:
        return self.cache.path("publish", f"instagram-{req.token}.json")

    def _save_state(self, req: PublishRequest, state: dict) -> None:
        Cache.write_json(self._state_path(req), state)

    def _finish(self, auth: dict, media_id: str) -> PublishResult:
        try:
            link = _graph("GET", f"/{media_id}", params={"fields": "permalink",
                                                         "access_token": auth["page_token"]}).get("permalink")
        except (PermanentPublishError, RetryablePublishError):
            link = None
        return PublishResult(media_id, link or profile_url(auth.get("username")) or "https://www.instagram.com/")

    def _status(self, auth: dict, container_id: str) -> dict:
        return _graph("GET", f"/{container_id}", params={"fields": "status_code,status",
                                                          "access_token": auth["page_token"]})

    def _wait_and_publish(self, req: PublishRequest, auth: dict, state: dict) -> PublishResult:
        deadline = self.clock() + POLL_TIMEOUT
        while True:
            status = self._status(auth, state["container_id"])
            code = status.get("status_code")
            if code == "FINISHED":
                break
            if code == "PUBLISHED":  # publié juste avant un arrêt : on retrouve le média
                media_id = self._find_published(auth, req)
                if media_id:
                    return self._finish(auth, media_id)
                return PublishResult(state["container_id"], profile_url(auth.get("username")) or "")
            if code in ("ERROR", "EXPIRED"):
                self._state_path(req).unlink(missing_ok=True)
                if code == "EXPIRED":
                    raise RetryablePublishError("Instagram : conteneur expiré, nouvel envoi")
                raise PermanentPublishError(f"Instagram a refusé la vidéo ({status.get('status') or 'erreur'})")
            if self.clock() > deadline:
                raise RetryablePublishError("Instagram traite encore la vidéo : nouvelle vérification plus tard")
            self.sleep(POLL_SECONDS)
        published = _graph("POST", f"/{auth['ig_user_id']}/media_publish",
                           params={"creation_id": state["container_id"], "access_token": auth["page_token"]})
        state["media_id"] = published["id"]
        self._save_state(req, state)
        return self._finish(auth, published["id"])

    def _find_published(self, auth: dict, req: PublishRequest) -> str | None:
        recent = _graph("GET", f"/{auth['ig_user_id']}/media", params={
            "fields": "id,caption", "limit": 10, "access_token": auth["page_token"]}).get("data", [])
        text = caption(req)[:80]
        return next((m["id"] for m in recent if (m.get("caption") or "").startswith(text)), None)

    def find_existing(self, req: PublishRequest, account: Account) -> PublishResult | None:
        path = self._state_path(req)
        if not path.exists():
            return None
        state, auth = Cache.read_json(path), self._auth(account)
        if state.get("media_id"):
            return self._finish(auth, state["media_id"])
        if not state.get("uploaded"):
            path.unlink(missing_ok=True)  # envoi interrompu : nouveau conteneur
            return None
        return self._wait_and_publish(req, auth, state)

    def publish(self, req: PublishRequest, account: Account) -> PublishResult:
        auth = self._auth(account)
        size = req.video_path.stat().st_size
        if size > MAX_BYTES:
            raise PermanentPublishError("Instagram : vidéo de plus de 300 Mo, refusée pour un Reel")
        container = _graph("POST", f"/{auth['ig_user_id']}/media", params={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption(req),
            "access_token": auth["page_token"]})
        state = {"container_id": container["id"], "uploaded": False}
        self._save_state(req, state)
        upload_url = container.get("uri") or f"{UPLOAD}/{container['id']}"
        try:
            request("POST", upload_url, data=req.video_path.read_bytes(), timeout=900, headers={
                "Authorization": f"OAuth {auth['page_token']}", "offset": "0", "file_size": str(size)})
        except HTTPStatusError as exc:
            info = exc.data.get("debug_info") or {}
            self._state_path(req).unlink(missing_ok=True)
            if info.get("retriable"):
                raise RetryablePublishError(f"Instagram : envoi interrompu ({info.get('message', exc)})") from exc
            raise PermanentPublishError(f"Instagram : envoi refusé ({info.get('message') or exc})") from exc
        state["uploaded"] = True
        self._save_state(req, state)
        return self._wait_and_publish(req, auth, state)


def caption(req: PublishRequest) -> str:
    """Légende : titre, description, hashtags (2 200 caractères, 30 hashtags au plus)."""
    tags = " ".join(f"#{t.lstrip('#').replace(' ', '')}" for t in req.tags[:30] if t.strip())
    parts = [req.title.strip(), req.description.strip(), tags]
    return "\n\n".join(p for p in parts if p)[:2200]
