"""Fournisseurs d'IA : Claude (API Anthropic), Gemini, OpenAI, Mistral (API HTTP),
Ollama (local) et un faux pour les tests."""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request

from pydantic import ValidationError

from .. import secrets
from .base import (SYSTEM_PROMPT, AIProvider, AIProviderError, AIRefusal, ScoringRequest,
                   ScoringResult, SegmentScore, T, Usage, json_schema, rough_token_count)
from .remote import DEFAULT_REMOTE_MODELS, GeminiProvider, MistralProvider, OpenAIProvider

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5-5",
    "ollama": "qwen2.5:3b",
    "fake": "fake-1",
    **DEFAULT_REMOTE_MODELS,
}

TEXT_PROVIDERS = ("anthropic", "gemini", "openai", "mistral", "ollama", "fake")


class AnthropicProvider(AIProvider):
    """Claude via le SDK officiel : sortie structurée validée par pydantic,
    prompt système mis en cache (identique d'un appel à l'autre)."""

    name = "anthropic"

    def __init__(self, model: str | None = None, effort: str = "low") -> None:
        import anthropic

        self.model = model or DEFAULT_MODELS["anthropic"]
        self.effort = effort
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key("anthropic"), max_retries=3)

    @staticmethod
    def _params(system: str, user: str, model: str, images: list[tuple[str, bytes]] | None = None) -> dict:
        content: list[dict] = [{"type": "text", "text": user}]
        for label, data in images or []:
            if label:
                content.append({"type": "text", "text": label})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                        "data": base64.b64encode(data).decode()}})
        return {
            "model": model,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content}],
        }

    def estimate_tokens(self, request: ScoringRequest) -> int:
        try:
            counted = self.client.messages.count_tokens(
                **self._params(SYSTEM_PROMPT, request.user_content(), self.model,
                               request.image_list())).input_tokens
        except self._anthropic.APIError:
            return super().estimate_tokens(request)
        return counted + self.output_tokens_per_segment * len(request.segments)

    def complete_json(self, system: str, user: str, schema: type[T], *,
                      max_tokens: int = 4000, images: list[tuple[str, bytes]] | None = None) -> tuple[T, Usage]:
        a = self._anthropic
        try:
            response = self.client.messages.parse(
                max_tokens=max_tokens,
                output_format=schema,
                output_config={"effort": self.effort},
                **self._params(system, user, self.model, images),
            )
        except (a.AuthenticationError, a.PermissionDeniedError, a.NotFoundError, a.BadRequestError) as exc:
            raise AIProviderError(f"Anthropic : {exc}") from exc
        # RateLimitError, 5xx et erreurs réseau remontent : la file de jobs réessaiera.
        u = response.usage
        usage = Usage(
            input_tokens=u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0),
            output_tokens=u.output_tokens,
        )
        if response.stop_reason == "refusal":
            raise AIRefusal("Claude a refusé de répondre")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise RuntimeError(f"réponse incomplète ({response.stop_reason})")
        return response.parsed_output, usage

    def list_models(self) -> list[str]:
        a = self._anthropic
        try:
            return [m.id for m in self.client.models.list(limit=100)]
        except (a.AuthenticationError, a.PermissionDeniedError) as exc:
            raise AIProviderError(f"Anthropic : clé refusée ({exc.status_code})") from exc
        except a.APIError as exc:  # réseau, surcharge
            raise RuntimeError(f"Anthropic : {exc}") from exc


class OllamaProvider(AIProvider):
    """Modèle local servi par Ollama (http://127.0.0.1:11434), sortie contrainte
    par le schéma JSON. Gratuit, mais lent sur CPU."""

    name = "ollama"
    supports_images = False  # la plupart des modèles locaux légers ne lisent pas les images

    def __init__(self, model: str | None = None, url: str = "http://127.0.0.1:11434") -> None:
        self.model = model or DEFAULT_MODELS["ollama"]
        self.url = url

    def complete_json(self, system: str, user: str, schema: type[T], *,
                      max_tokens: int = 4000, images: list[tuple[str, bytes]] | None = None) -> tuple[T, Usage]:
        body = {
            "model": self.model,
            "stream": False,
            "format": json_schema(schema),
            "options": {"temperature": 0, "num_predict": max_tokens},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        req = urllib.request.Request(f"{self.url}/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise AIProviderError(f"modèle Ollama absent : `ollama pull {self.model}`") from exc
            raise
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama injoignable sur {self.url} : {exc.reason}") from exc
        usage = Usage(data.get("prompt_eval_count", 0), data.get("eval_count", 0))
        try:
            return schema.model_validate_json(data["message"]["content"]), usage
        except (ValidationError, KeyError) as exc:
            raise RuntimeError(f"JSON invalide renvoyé par Ollama : {exc}") from exc

    def list_models(self) -> list[str]:
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags", timeout=10) as resp:
                return [m["name"] for m in json.load(resp).get("models", [])]
        except urllib.error.URLError as exc:
            raise AIProviderError(f"Ollama injoignable sur {self.url}") from exc


class FakeProvider(AIProvider):
    """Aucun appel réseau, résultat déterministe. Utilisé en dry run et en test ;
    garde la trace de ce qui lui a été envoyé."""

    name = "fake"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or DEFAULT_MODELS["fake"]
        self.calls: list[ScoringRequest] = []
        self.json_calls: list[tuple[str, str, type]] = []
        self.images_seen = 0

    def score(self, request: ScoringRequest) -> tuple[ScoringResult, Usage]:
        self.calls.append(request)
        self.images_seen += len(request.image_list())
        results = []
        for seg in request.segments:
            jitter = int(hashlib.sha256(seg.id.encode()).hexdigest()[:2], 16) % 10
            score = max(0, min(100, int(55 + seg.signal_score * 10 + jitter)))
            words = seg.text.split()
            results.append(SegmentScore(
                id=seg.id, score=score,
                reason=f"[simulé] pic de {seg.signal} d'intensité {seg.signal_score:.1f}",
                title=" ".join(words[:8])[:70] or "Extrait",
                description=" ".join(words[:25]),
                hook=" ".join(words[:5])[:40] or "Regarde bien la fin",
            ))
        output = ScoringResult(segments=results)
        return output, Usage(rough_token_count(SYSTEM_PROMPT + request.user_content()),
                             rough_token_count(output.model_dump_json()))

    def complete_json(self, system: str, user: str, schema: type[T], *,
                      max_tokens: int = 4000, images: list[tuple[str, bytes]] | None = None) -> tuple[T, Usage]:
        """Les schémas qui veulent un faux résultat exposent `fake(user)`."""
        self.json_calls.append((system, user, schema))
        fake = getattr(schema, "fake", None)
        if fake is None:
            raise AIProviderError(f"pas de réponse simulée pour {schema.__name__}")
        output = fake(user)
        return output, Usage(rough_token_count(system + user), rough_token_count(output.model_dump_json()))

    def list_models(self) -> list[str]:
        return [self.model]


def api_key(provider: str) -> str:
    creds = secrets.get_json(secrets.credentials_key(provider))
    if not creds or not creds.get("api_key"):
        raise AIProviderError(f"clé d'API {provider} absente : Paramètres > Clés d'API")
    return creds["api_key"]


def make_text_provider(provider: str, model: str | None = None, *, effort: str = "low") -> AIProvider:
    if provider == "fake":
        return FakeProvider(model)
    if provider == "anthropic":
        return AnthropicProvider(model, effort=effort)
    if provider == "ollama":
        return OllamaProvider(model)
    if provider == "gemini":
        return GeminiProvider(api_key("gemini"), model)
    if provider == "openai":
        return OpenAIProvider(api_key("openai"), model)
    if provider == "mistral":
        return MistralProvider(api_key("mistral"), model)
    raise AIProviderError(f"fournisseur d'IA inconnu : {provider!r}")


def make_provider(settings: dict, *, dry_run: bool) -> AIProvider | None:
    """Fournisseur configuré pour un bot. En dry run : toujours le faux, aucun appel payant."""
    provider = settings.get("provider", "none")
    if provider == "none":
        return None
    if dry_run:
        return FakeProvider()
    return make_text_provider(provider, settings.get("model"), effort=settings.get("effort", "low"))
