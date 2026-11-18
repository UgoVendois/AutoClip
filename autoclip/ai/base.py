"""Interface commune des fournisseurs d'IA et format de sortie validé."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

from pydantic import BaseModel, Field

from ..jobs.queue import PermanentError

PROMPT_VERSION = 1

SYSTEM_PROMPT = """Tu sélectionnes des extraits de vidéos (streams Twitch, vidéos YouTube) à publier en Shorts verticaux de 20 à 60 secondes.

On te donne des segments déjà présélectionnés par des signaux bon marché (pics du chat, horodatages cités en commentaire, énergie audio). Pour chaque segment, tu as sa transcription et l'intensité du signal.

Pour chaque segment, renvoie :
- score : entier de 0 à 100. 80+ = moment fort qui tient seul sans contexte (chute, réaction, punchline, révélation, action spectaculaire). 50-79 = intéressant mais dépend du contexte. Moins de 50 = rien de notable, propos de remplissage, ou incompréhensible sans le reste de la vidéo.
- reason : une phrase en français qui justifie le score à partir du contenu.
- title : titre accrocheur en français, 70 caractères maximum, sans hashtag, sans guillemets, fidèle au contenu (pas de promesse que le segment ne tient pas).
- description : 1 à 2 phrases en français qui donnent envie de regarder.

Juge seulement ce que dit la transcription : un pic de chat sans contenu exploitable dans la transcription mérite un score bas. Reprends exactement les identifiants (id) reçus, un résultat par segment."""


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


class AIProvider(ABC):
    name: str
    model: str
    # Sortie attendue par segment, pour estimer le coût avant l'appel.
    output_tokens_per_segment = 120

    @abstractmethod
    def estimate_tokens(self, request: ScoringRequest) -> int:
        """Estimation entrée + sortie, avant tout appel payant."""

    @abstractmethod
    def score(self, request: ScoringRequest) -> tuple[ScoringResult, Usage]:
        """Note les segments en un seul appel."""


def validate_result(request: ScoringRequest, result: ScoringResult) -> dict[str, SegmentScore]:
    """Garde un résultat par segment demandé ; les segments oubliés valent 0."""
    wanted = {s.id for s in request.segments}
    by_id = {r.id: r for r in result.segments if r.id in wanted}
    for seg_id in wanted - by_id.keys():
        by_id[seg_id] = SegmentScore(id=seg_id, score=0, reason="non noté par le modèle",
                                     title="", description="")
    return by_id


def rough_token_count(text: str) -> int:
    """Approximation prudente (≈ 3,5 caractères par token pour du français)."""
    return int(len(text) / 3.5) + 1
