"""Interface commune des fournisseurs d'IA et format de sortie validé."""

from __future__ import annotations

import copy
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel, Field

from ..jobs.queue import PermanentError

PROMPT_VERSION = 2

SYSTEM_PROMPT = """Tu sélectionnes des extraits de vidéos (streams Twitch, vidéos YouTube, souvent du jeu vidéo) à publier en Shorts verticaux.

On te donne des segments déjà présélectionnés par des signaux bon marché (pics du chat, horodatages cités en commentaire, énergie audio). Pour chaque segment : sa transcription, l'intensité du signal et, quand elles sont fournies, trois images (début, moment fort, fin) annoncées par « Images du segment <id> ».

Pour chaque segment, renvoie :
- score : entier de 0 à 100. 80+ = moment fort qui tient seul sans contexte : action spectaculaire à l'écran (crash, cascade, poursuite, bug absurde), réaction forte du streamer (rire, rage, choc), punchline, révélation. 50-79 = intéressant mais dépend du contexte. Moins de 50 = rien de notable, remplissage, menu, écran de chargement, ou incompréhensible sans le reste.
- reason : une phrase en français qui justifie le score à partir de ce qui se passe (paroles et images).
- title : titre accrocheur en français, 70 caractères maximum, sans hashtag ni guillemets, fidèle au contenu.
- description : 1 à 2 phrases en français qui donnent envie de regarder.
- hook : texte d'accroche incrusté à l'écran pendant les 3 premières secondes, en français, 6 à 40 caractères, qui donne envie de voir la suite sans tout révéler (ex. « Il ne s'attendait pas à ça », « Le pire braquage de l'histoire »). Pas d'emoji, pas de hashtag.

Juge le contenu réel : un pic de chat sans rien de notable à l'écran ni dans les paroles mérite un score bas. Un segment sans parole peut valoir 90 si l'action à l'écran est spectaculaire. Reprends exactement les identifiants (id) reçus, un résultat par segment."""


@dataclass(frozen=True)
class Segment:
    id: str
    start: float
    end: float
    text: str
    signal: str  # chat | comments | audio | heuristic
    signal_score: float


@dataclass(frozen=True)
class ScoringRequest:
    video_title: str
    channel: str
    segments: list[Segment]
    # Images clés par segment (JPEG), envoyées avec le texte si le fournisseur sait les lire.
    images: dict[str, list[bytes]] = field(default_factory=dict)

    def image_list(self) -> list[tuple[str, bytes]]:
        """(légende, JPEG) dans l'ordre des segments."""
        out = []
        for seg in self.segments:
            for i, data in enumerate(self.images.get(seg.id, [])):
                label = f"Images du segment {seg.id}" if i == 0 else ""
                out.append((label, data))
        return out

    def user_content(self) -> str:
        payload = {
            "video": {"title": self.video_title, "channel": self.channel},
            "segments": [
                {"id": s.id, "duration_s": round(s.end - s.start), "signal": s.signal,
                 "signal_strength": round(s.signal_score, 2), "transcript": s.text}
                for s in self.segments
            ],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class SegmentScore(BaseModel):
    id: str
    score: int = Field(ge=0, le=100)
    reason: str
    title: str
    description: str
    hook: str


class ScoringResult(BaseModel):
    segments: list[SegmentScore]


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class AIProviderError(PermanentError):
    """Erreur de configuration (clé absente, modèle inconnu) : inutile de réessayer."""


class AIRefusal(AIProviderError):
    """Le modèle a refusé de répondre (filtre de sécurité)."""


T = TypeVar("T", bound=BaseModel)


class AIProvider(ABC):
    name: str
    model: str
    # Sortie attendue par segment, pour estimer le coût avant l'appel.
    output_tokens_per_segment = 120

    # Une image 512x288 coûte ~200 tokens chez Claude ; marge pour les autres.
    tokens_per_image = 300
    supports_images = True

    def estimate_tokens(self, request: ScoringRequest) -> int:
        """Estimation entrée + sortie, avant tout appel payant."""
        images = len(request.image_list()) if self.supports_images else 0
        return (rough_token_count(SYSTEM_PROMPT + request.user_content())
                + self.tokens_per_image * images
                + self.output_tokens_per_segment * len(request.segments))

    @abstractmethod
    def complete_json(self, system: str, user: str, schema: type[T], *,
                      max_tokens: int = 4000, images: list[tuple[str, bytes]] | None = None) -> tuple[T, Usage]:
        """Un appel, réponse JSON conforme au modèle pydantic `schema`. `images` :
        (légende, JPEG) placées après le texte. Lève AIRefusal, AIProviderError
        (définitif) ou une autre exception (réessayable)."""

    def score(self, request: ScoringRequest) -> tuple[ScoringResult, Usage]:
        """Note les segments en un seul appel."""
        max_tokens = 2000 + self.output_tokens_per_segment * len(request.segments) * 2
        try:
            return self.complete_json(SYSTEM_PROMPT, request.user_content(), ScoringResult,
                                      max_tokens=max_tokens,
                                      images=request.image_list() if self.supports_images else None)
        except AIRefusal:
            return ScoringResult(segments=[]), Usage(0, 0)  # tous les segments vaudront 0


def validate_result(request: ScoringRequest, result: ScoringResult) -> dict[str, SegmentScore]:
    """Garde un résultat par segment demandé ; les segments oubliés valent 0."""
    wanted = {s.id for s in request.segments}
    by_id = {r.id: r for r in result.segments if r.id in wanted}
    for seg_id in wanted - by_id.keys():
        by_id[seg_id] = SegmentScore(id=seg_id, score=0, reason="non noté par le modèle",
                                     title="", description="", hook="")
    return by_id


def rough_token_count(text: str) -> int:
    """Approximation prudente (≈ 3,5 caractères par token pour du français)."""
    return int(len(text) / 3.5) + 1


def json_schema(model: type[BaseModel], *, strict: bool = False) -> dict:
    """Schéma JSON autonome (références `$defs` développées), accepté par toutes
    les API. `strict` : objets fermés et tous les champs requis (OpenAI)."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]]))
            # "title" texte = libellé pydantic, inutile ; un champ nommé "title" est gardé.
            out = {k: walk(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
            if strict:
                out.pop("default", None)  # mot-clé refusé en mode strict
            if out.get("type") == "object" and strict:
                out["additionalProperties"] = False
                out["required"] = list(out.get("properties", {}))
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)
