"""Vérification des clés d'API enregistrées : un appel gratuit et sans effet par
plateforme (liste de modèles, jeton d'application, lecture à 1 unité de quota).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

from . import secrets

TIMEOUT = 20


@dataclass
class CheckResult:
    ok: bool
    message: str
    # False : la clé n'a pu être vérifiée qu'en partie (format, réseau...).
    conclusive: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def _request(method: str, url: str, *, form: dict | None = None,
             headers: dict | None = None) -> tuple[int, dict]:
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if form is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        with exc:
            status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw or b"{}")
    except ValueError:
        return status, {"raw": raw[:200].decode(errors="replace")}


def _error_text(body: dict) -> str:
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("status") or err)
    return str(body.get("error_description") or body.get("message") or err or body)[:200]


# -- par plateforme ------------------------------------------------------------------

def _ai(platform: str) -> CheckResult:
    from .ai.base import AIProviderError
    from .ai.providers import make_text_provider

    try:
        models = make_text_provider(platform).list_models()
    except AIProviderError as exc:
        return CheckResult(False, str(exc))
    return CheckResult(True, f"Clé valide : {len(models)} modèle{'s' if len(models) > 1 else ''} accessible{'s' if len(models) > 1 else ''}.")


def _youtube(creds: dict) -> CheckResult:
    # Échange d'un code bidon : Google répond invalid_client si l'identifiant ou le
    # secret est faux, invalid_grant s'ils sont bons (le code, lui, est forcément faux).
    status, body = _request("POST", "https://oauth2.googleapis.com/token", form={
        "client_id": creds["client_id"], "client_secret": creds["client_secret"],
        "code": "autoclip-key-check", "grant_type": "authorization_code",
        "redirect_uri": "http://localhost"})
    error = body.get("error")
    if error == "invalid_grant":
        return CheckResult(True, "Identifiant OAuth valide. Connecte une chaîne pour finir.")
    if error in ("invalid_client", "unauthorized_client"):
        return CheckResult(False, f"Identifiant ou secret refusé par Google ({_error_text(body)}).")
    return CheckResult(False, f"Réponse inattendue de Google ({status} : {_error_text(body)}).", conclusive=False)


GOOGLE_KEY_ERRORS = {
    "API_KEY_INVALID": "Clé inconnue de Google : vérifie qu'elle est copiée en entier.",
    "SERVICE_DISABLED": "Clé reconnue, mais YouTube Data API v3 n'est pas activée sur le projet de cette clé "
                        "(attention à bien choisir le projet en haut de la console).",
    "API_KEY_SERVICE_BLOCKED": "Clé reconnue, mais ses restrictions d'API excluent YouTube Data API v3 : "
                               "ajoute-la dans Identifiants > ta clé > Restrictions relatives aux API.",
    "API_KEY_HTTP_REFERRER_BLOCKED": "Clé limitée à des sites web (référents HTTP) : mets « Aucune » dans "
                                     "Restrictions relatives aux applications.",
    "API_KEY_IP_ADDRESS_BLOCKED": "Clé limitée à certaines adresses IP : mets « Aucune » dans Restrictions "
                                  "relatives aux applications, ou ajoute ton IP.",
    "API_KEY_ANDROID_APP_BLOCKED": "Clé réservée à une app Android : mets « Aucune » dans Restrictions "
                                   "relatives aux applications.",
    "API_KEY_IOS_APP_BLOCKED": "Clé réservée à une app iOS : mets « Aucune » dans Restrictions relatives "
                               "aux applications.",
    "ACCESS_DENIED": "Google refuse cette clé pour tous les services. Solution qui marche le plus souvent : "
                     "crée une nouvelle clé dans le projet où YouTube Data API v3 est activée (sans cocher "
                     "« lier à un compte de service »), puis remplace-la ici. Une clé créée avant l'activation "
                     "des API, ou liée à un compte de service, reste souvent bloquée.",
}


def _google_reason(body: dict) -> str | None:
    err = body.get("error") or {}
    for detail in err.get("details", []) if isinstance(err, dict) else []:
        if detail.get("reason"):
            return detail["reason"]
    return None


def _youtube_data(creds: dict) -> CheckResult:
    query = urllib.parse.urlencode({"part": "id", "id": "jNQXAC9IVRw", "key": creds["api_key"].strip()})
    status, body = _request("GET", f"https://www.googleapis.com/youtube/v3/videos?{query}")
    if status == 200:
        return CheckResult(True, "Clé valide, API YouTube Data v3 activée.")
    reason = _google_reason(body)
    message = _error_text(body)
    if reason in GOOGLE_KEY_ERRORS:
        return CheckResult(False, f"{GOOGLE_KEY_ERRORS[reason]} (code Google : {reason})")
    if "has not been used" in message or "disabled" in message.lower():
        return CheckResult(False, GOOGLE_KEY_ERRORS["SERVICE_DISABLED"])
    if status == 403 and "quota" in message.lower():
        return CheckResult(True, "Clé valide, mais le quota YouTube du jour est épuisé.")
    return CheckResult(False, f"Clé refusée ({status} : {message}{f', code {reason}' if reason else ''}).")


def _twitch(creds: dict) -> CheckResult:
    status, body = _request("POST", "https://id.twitch.tv/oauth2/token", form={
        "client_id": creds["client_id"], "client_secret": creds["client_secret"],
        "grant_type": "client_credentials"})
    if status == 200 and body.get("access_token"):
        return CheckResult(True, "Client ID et secret valides.")
    return CheckResult(False, f"Refusé par Twitch ({status} : {_error_text(body)}).")


def _tiktok(creds: dict) -> CheckResult:
    status, body = _request("POST", "https://open.tiktokapis.com/v2/oauth/token/", form={
        "client_key": creds["client_key"], "client_secret": creds["client_secret"],
        "grant_type": "client_credentials"})
    if status == 200 and body.get("access_token"):
        return CheckResult(True, "Clés valides.")
    return CheckResult(False, f"Refusé par TikTok ({status} : {_error_text(body)}).")


def _instagram(creds: dict) -> CheckResult:
    query = urllib.parse.urlencode({"client_id": creds["app_id"], "client_secret": creds["app_secret"],
                                    "grant_type": "client_credentials"})
    status, body = _request("GET", f"https://graph.facebook.com/oauth/access_token?{query}")
    if status == 200 and body.get("access_token"):
        return CheckResult(True, "App ID et secret valides.")
    return CheckResult(False, f"Refusé par Meta ({status} : {_error_text(body)}).")


CHECKERS = {
    "youtube": _youtube,
    "youtube_data": _youtube_data,
    "twitch": _twitch,
    "tiktok": _tiktok,
    "instagram": _instagram,
}
AI_PLATFORMS = ("anthropic", "gemini", "openai", "mistral")


def check(platform: str) -> CheckResult:
    if platform not in secrets.CREDENTIAL_FIELDS:
        raise ValueError(f"plateforme inconnue : {platform}")
    creds = secrets.get_json(secrets.credentials_key(platform))
    if not creds:
        return CheckResult(False, "Aucune clé enregistrée.")
    try:
        if platform in AI_PLATFORMS:
            return _ai(platform)
        return CHECKERS[platform](creds)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return CheckResult(False, f"Vérification impossible : réseau ({reason}).", conclusive=False)
    except RuntimeError as exc:  # fournisseur d'IA : 429, 5xx, réseau
        return CheckResult(False, f"Vérification impossible pour l'instant : {exc}", conclusive=False)
