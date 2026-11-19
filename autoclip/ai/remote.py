"""Fournisseurs d'IA joignables par simple HTTP (bibliothèque standard) :
Gemini (Google), OpenAI et Mistral.

Les noms de modèles changent souvent : ceux d'ici ne sont que des valeurs par
défaut, l'interface propose la liste renvoyée par chaque API (`list_models`).
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from pydantic import ValidationError

from .base import AIProvider, AIProviderError, AIRefusal, T, Usage, json_schema

DEFAULT_REMOTE_MODELS = {
    "gemini": "gemini-flash-latest",
    "openai": "gpt-6.2-sol",
    "mistral": "mistral-large-latest",
}

PERMANENT_STATUS = {400, 401, 403, 404, 422}
TIMEOUT = 300


def http_json(method: str, url: str, *, headers: dict[str, str], body: dict | None = None,
              provider: str, timeout: float = TIMEOUT) -> dict:
    """Requête JSON. Erreurs 4xx de configuration -> AIProviderError (pas de
    nouvel essai) ; 429, 5xx et réseau -> RuntimeError (la file réessaiera)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        if exc.code == 429 and _no_free_quota(detail):
            raise AIProviderError(
                f"{provider} : ce modèle n'a aucun quota gratuit (limite à 0 dans l'offre gratuite). "
                "Active la facturation du projet sur aistudio.google.com (Paramètres > Facturation), "
                "ou choisis un autre modèle ou fournisseur (le mode « SVG » des visuels est gratuit).") from exc
        if exc.code in PERMANENT_STATUS:
            raise AIProviderError(f"{provider} {exc.code} : {detail}") from exc
        raise RuntimeError(f"{provider} {exc.code} : {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{provider} injoignable : {exc.reason}") from exc


def _no_free_quota(detail: str) -> bool:
    """Quota gratuit à 0 pour ce modèle (et non un quota épuisé) : réessayer ne sert à rien."""
    return "free_tier" in detail and "limit: 0" in detail


def _error_detail(exc: urllib.error.HTTPError, limit: int = 1500) -> str:
    try:
        with exc:
            raw = exc.read().decode(errors="replace")
    except Exception:  # noqa: BLE001
        return exc.reason or ""
    try:
        err = json.loads(raw).get("error", raw)
        if isinstance(err, dict):
            return str(err.get("message") or err)[:limit]
        return str(err)[:limit]
    except (ValueError, AttributeError):
        return raw[:limit]


def _parse(schema: type[T], text: str, provider: str) -> T:
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise RuntimeError(f"JSON invalide renvoyé par {provider} : {exc}") from exc


# -- Gemini ---------------------------------------------------------------------

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str | None = None, base_url: str = GEMINI_URL) -> None:
        self.key = api_key
        self.model = model or DEFAULT_REMOTE_MODELS["gemini"]
        self.base_url = base_url

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.key}

    def complete_json(self, system: str, user: str, schema: type[T], *,
                      max_tokens: int = 4000, images: list[tuple[str, bytes]] | None = None) -> tuple[T, Usage]:
        parts: list[dict] = [{"text": user}]
        for label, data in images or []:
            if label:
                parts.append({"text": label})
            parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(data).decode()}})
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": json_schema(schema),
                "maxOutputTokens": max_tokens,
            },
        }
        data = http_json("POST", f"{self.base_url}/models/{self.model}:generateContent",
                         headers=self._headers(), body=body, provider="Gemini")
        meta = data.get("usageMetadata", {})
        usage = Usage(meta.get("promptTokenCount", 0),
                      meta.get("candidatesTokenCount", 0) + meta.get("thoughtsTokenCount", 0))
        if data.get("promptFeedback", {}).get("blockReason"):
            raise AIRefusal(f"Gemini a bloqué la demande ({data['promptFeedback']['blockReason']})")
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini : réponse vide")
        reason = candidates[0].get("finishReason")
        if reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION"):
            raise AIRefusal(f"Gemini a refusé de répondre ({reason})")
        if reason == "MAX_TOKENS":
            raise RuntimeError("Gemini : réponse incomplète (MAX_TOKENS)")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return _parse(schema, text, "Gemini"), usage

    def list_models(self) -> list[str]:
        data = http_json("GET", f"{self.base_url}/models?pageSize=1000", headers=self._headers(),
                         provider="Gemini", timeout=20)
        return sorted(m["name"].removeprefix("models/") for m in data.get("models", [])
                      if "generateContent" in m.get("supportedGenerationMethods", []))


# -- OpenAI et Mistral (API « chat completions ») --------------------------------------

class _ChatCompletionsProvider(AIProvider):
    base_url: str
    label: str
    strict_schema = False

    def __init__(self, api_key: str, model: str | None = None, base_url: str | None = None) -> None:
        self.key = api_key
        self.model = model or DEFAULT_REMOTE_MODELS[self.name]
        if base_url:
            self.base_url = base_url

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    def _token_limit(self, max_tokens: int) -> dict:
        return {"max_tokens": max_tokens}

    @staticmethod
    def _image_part(data_uri: str) -> dict:
        return {"type": "image_url", "image_url": {"url": data_uri}}

    def complete_json(self, system: str, user: str, schema: type[T], *,
                      max_tokens: int = 4000, images: list[tuple[str, bytes]] | None = None) -> tuple[T, Usage]:
        content: str | list[dict] = user
        if images:
            content = [{"type": "text", "text": user}]
            for label, data in images:
                if label:
                    content.append({"type": "text", "text": label})
                content.append(self._image_part(f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"))
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": schema.__name__,
                "schema": json_schema(schema, strict=self.strict_schema),
                "strict": self.strict_schema,
            }},
            **self._token_limit(max_tokens),
        }
        data = http_json("POST", f"{self.base_url}/chat/completions", headers=self._headers(),
                         body=body, provider=self.label)
        u = data.get("usage") or {}
        usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"{self.label} : réponse vide")
        message = choices[0].get("message", {})
        if message.get("refusal"):
            raise AIRefusal(f"{self.label} a refusé de répondre : {message['refusal']}")
        if choices[0].get("finish_reason") == "length":
            raise RuntimeError(f"{self.label} : réponse incomplète (limite de tokens)")
        if choices[0].get("finish_reason") == "content_filter":
            raise AIRefusal(f"{self.label} : réponse bloquée par le filtre de contenu")
        content = message.get("content") or ""
        if isinstance(content, list):  # Mistral peut renvoyer des blocs
            content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        return _parse(schema, content, self.label), usage

    def list_models(self) -> list[str]:
        data = http_json("GET", f"{self.base_url}/models", headers=self._headers(),
                         provider=self.label, timeout=20)
        return sorted({m["id"] for m in data.get("data", [])})


class OpenAIProvider(_ChatCompletionsProvider):
    name = "openai"
    label = "OpenAI"
    base_url = "https://api.openai.com/v1"
    strict_schema = True

    def _token_limit(self, max_tokens: int) -> dict:
        return {"max_completion_tokens": max_tokens}


class MistralProvider(_ChatCompletionsProvider):
    name = "mistral"
    label = "Mistral"
    base_url = "https://api.mistral.ai/v1"

    @staticmethod
    def _image_part(data_uri: str) -> dict:
        return {"type": "image_url", "image_url": data_uri}  # format Mistral : URL en chaîne
