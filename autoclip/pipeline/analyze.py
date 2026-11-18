"""Étape analyze : choisit les meilleurs extraits dans la transcription.

Un extrait commence au début d'une phrase et se termine à la fin d'une phrase.
Le score (heuristique, sans LLM pour la phase 1) favorise :
- un débit de parole soutenu et peu de blancs ;
- une accroche au début (question, mot d'accroche) ;
- des phrases expressives (points d'exclamation et d'interrogation).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

END_PUNCT = (".", "?", "!", "…", "。", "？", "！")
HOOKS = (
    "pourquoi", "comment", "imagine", "imaginez", "le secret", "personne", "jamais", "voici",
    "attention", "incroyable", "la vérité", "saviez-vous", "tu savais",
    "why", "how", "what if", "nobody", "never", "here's", "the secret", "imagine", "did you know",
)


@dataclass
class Sentence:
    words: list[dict]

    @property
    def start(self) -> float:
        return self.words[0]["start"]

    @property
    def end(self) -> float:
        return self.words[-1]["end"]

    @property
    def text(self) -> str:
        return "".join(w["word"] for w in self.words).strip()


@dataclass
class Candidate:
    start: float
    end: float
    score: float
    sentences: list[Sentence] = field(repr=False)

    @property
    def words(self) -> list[dict]:
        return [w for s in self.sentences for w in s.words]

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.sentences)

    @property
    def title(self) -> str:
        first = self.sentences[0].text
        title = first if len(first) <= 90 else first[:87].rsplit(" ", 1)[0] + "…"
        return title


def split_sentences(words: list[dict], max_gap: float = 1.2) -> list[Sentence]:
    """Coupe à chaque ponctuation de fin de phrase, ou sur un long silence."""
    sentences: list[Sentence] = []
    current: list[dict] = []
    for word in words:
        if current and word["start"] - current[-1]["end"] > max_gap:
            sentences.append(Sentence(current))
            current = []
        current.append(word)
        if word["word"].strip().endswith(END_PUNCT):
            sentences.append(Sentence(current))
            current = []
    if current:
        sentences.append(Sentence(current))
    return sentences


def score(sentences: list[Sentence]) -> float:
    words = [w for s in sentences for w in s.words]
    duration = max(sentences[-1].end - sentences[0].start, 0.1)
    density = min(len(words) / duration / 3.0, 1.0)  # ~3 mots/s = débit soutenu
    spoken = sum(w["end"] - w["start"] for w in words)
    silence = max(0.0, 1.0 - spoken / duration)
    first = sentences[0].text.lower()
    hook = 0.0
    if first.endswith("?"):
        hook += 0.3
    if any(re.search(rf"\b{re.escape(h)}\b", first) for h in HOOKS):
        hook += 0.2
    expressive = sum(1 for s in sentences if s.text.endswith(("!", "?"))) / len(sentences)
    clean_end = 0.1 if sentences[-1].text.endswith(END_PUNCT) else 0.0
    return round(density - 0.5 * silence + hook + 0.3 * expressive + clean_end, 4)


def find_clips(words: list[dict], *, min_seconds: float = 20, max_seconds: float = 60,
               max_clips: int = 5) -> list[Candidate]:
    """Meilleurs extraits sans chevauchement, triés par ordre chronologique."""
    sentences = split_sentences(words)
    candidates: list[Candidate] = []
    for i in range(len(sentences)):
        for j in range(i, len(sentences)):
            duration = sentences[j].end - sentences[i].start
            if duration > max_seconds:
                break
            if duration >= min_seconds:
                chunk = sentences[i:j + 1]
                candidates.append(Candidate(chunk[0].start, chunk[-1].end, score(chunk), chunk))

    chosen: list[Candidate] = []
    for cand in sorted(candidates, key=lambda c: (-c.score, c.start)):
        if len(chosen) >= max_clips:
            break
        if all(cand.end <= c.start or cand.start >= c.end for c in chosen):
            chosen.append(cand)
    return sorted(chosen, key=lambda c: c.start)
