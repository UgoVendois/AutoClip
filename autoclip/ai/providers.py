"""Fournisseurs d'IA : Claude (API Anthropic), Ollama (local) et un faux pour les tests."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request

from pydantic import ValidationError

from .. import secrets
from .base import (SYSTEM_PROMPT, AIProvider, AIProviderError, ScoringRequest, ScoringResult,
                   SegmentScore, Usage, rough_token_count)

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5-5",
    "ollama": "qwen2.5:3b",
    "fake": "fake-1",
}


class AnthropicProvider(AIProvider):
    """Claude via le SDK officiel : sortie structurée validée par pydantic,
    prompt système mis en cache (identique d'un appel à l'autre)."""

    name = "anthropic"

    def __init__(self, model: str | None = None, effort: str = "low") -> None:
        import anthropic

        creds = secrets.get_json(secrets.credentials_key("anthropic"))
        if not creds or not creds.get("api_key"):
            raise AIProviderError("clé Anthropic absente : `autoclip credentials set anthropic`")
        self.model = model or DEFAULT_MODELS["anthropic"]
        self.effort = effort
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=creds["api_key"], max_retries=3)

    def _params(self, request: ScoringRequest) -> dict:
        return {
            "model": self.model,
            "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": request.user_content()}],
        }

    def _max_tokens(self, request: ScoringRequest) -> int:
        # Marge pour la réflexion du modèle en plus du JSON.
        return 2000 + self.output_tokens_per_segment * len(request.segments) * 2

    def estimate_tokens(self, request: ScoringRequest) -> int:
        try:
            counted = self.client.messages.count_tokens(**self._params(request)).input_tokens
        except self._anthropic.APIError:
            counted = rough_token_count(SYSTEM_PROMPT + request.user_content())
        return counted + self.output_tokens_per_segment * len(request.segments)

    def score(self, request: ScoringRequest) -> tuple[ScoringResult, Usage]:
        a = self._anthropic
        try:
            response = self.client.messages.parse(
                max_tokens=self._max_tokens(request),
                output_format=ScoringResult,
                output_config={"effort": self.effort},
                **self._params(request),
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
            return ScoringResult(segments=[]), usage  # tous les segments vaudront 0
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise RuntimeError(f"réponse incomplète ({response.stop_reason})")
        return response.parsed_output, usage


class OllamaProvider(AIProvider):
    """Modèle local servi par Ollama (http://127.0.0.1:11434), sortie contrainte
    par le schéma JSON. Gratuit, mais lent sur CPU."""

    name = "ollama"

    def __init__(self, model: str | None = None, url: str = "http://127.0.0.1:11434") -> None:
        self.model = model or DEFAULT_MODELS["ollama"]
        self.url = url

    def estimate_tokens(self, request: ScoringRequest) -> int:
        return (rough_token_count(SYSTEM_PROMPT + request.user_content())
                + self.output_tokens_per_segment * len(request.segments))

    def score(self, request: ScoringRequest) -> tuple[ScoringResult, Usage]:
        body = {
            "model": self.model,
            "stream": False,
            "format": ScoringResult.model_json_schema(),
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": request.user_content()},
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
            return ScoringResult.model_validate_json(data["message"]["content"]), usage
        except (ValidationError, KeyError) as exc:
            raise RuntimeError(f"JSON invalide renvoyé par Ollama : {exc}") from exc


class FakeProvider(AIProvider):
    """Aucun appel réseau, résultat déterministe. Utilisé en dry run et en test ;
    garde la trace de ce qui lui a été envoyé."""

    name = "fake"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or DEFAULT_MODELS["fake"]
        self.calls: list[ScoringRequest] = []

    def estimate_tokens(self, request: ScoringRequest) -> int:
        return (rough_token_count(SYSTEM_PROMPT + request.user_content())
                + self.output_tokens_per_segment * len(request.segments))

    def score(self, request: ScoringRequest) -> tuple[ScoringResult, Usage]:
        self.calls.append(request)
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
            ))
        output = ScoringResult(segments=results)
        return output, Usage(rough_token_count(SYSTEM_PROMPT + request.user_content()),
                             rough_token_count(output.model_dump_json()))


def make_provider(settings: dict, *, dry_run: bool) -> AIProvider | None:
    """Fournisseur configuré pour un bot. En dry run : toujours le faux, aucun appel payant."""
    provider = settings.get("provider", "none")
    if provider == "none":
        return None
    if dry_run or provider == "fake":
        return FakeProvider()
    if provider == "anthropic":
        return AnthropicProvider(settings.get("model"), effort=settings.get("effort", "low"))
    if provider == "ollama":
        return OllamaProvider(settings.get("model"))
    raise AIProviderError(f"fournisseur d'IA inconnu : {provider!r}")
