"""Tri bon marché : trouver les moments forts sans aucun appel à l'IA.

Entrée : une liste d'instants (secondes depuis le début de la vidéo) où il se
passe quelque chose : messages du chat, horodatages cités en commentaire...
Sortie : quelques fenêtres autour des pics de densité, classées.
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Window:
    start: float
    end: float
    score: float  # intensité du pic (z-score de la densité)
    peak: float  # instant le plus dense

    @property
    def duration(self) -> float:
        return self.end - self.start


def density(events: list[float], duration: float, *, window: float = 30.0,
            step: float = 5.0) -> list[tuple[float, int]]:
    """Nombre d'événements dans une fenêtre glissante [t, t + window[,
    échantillonnée tous les `step` secondes. Renvoie (début de fenêtre, nombre)."""
    events = sorted(e for e in events if 0 <= e <= duration)
    series = []
    t = 0.0
    last_start = max(duration - window, 0.0)
    while t <= last_start + 1e-9:
        lo = bisect.bisect_left(events, t)
        hi = bisect.bisect_left(events, t + window)
        series.append((t, hi - lo))
        t += step
    return series


def find_peaks(series: list[tuple[float, float]], *, top_n: int, min_separation: float,
               z_threshold: float = 1.5) -> list[tuple[float, float]]:
    """Pics de la série : z-score >= seuil, puis suppression des voisins plus
    faibles à moins de `min_separation` secondes. Renvoie (instant, z-score)."""
    if len(series) < 3:
        return []
    values = [v for _, v in series]
    mean = sum(values) / len(values)
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    if std == 0:
        return []  # activité uniforme : aucun moment ne se détache
    candidates = []
    for i, (t, v) in enumerate(series):
        z = (v - mean) / std
        left = values[i - 1] if i > 0 else -math.inf
        right = values[i + 1] if i + 1 < len(values) else -math.inf
        if z >= z_threshold and v >= left and v >= right:
            candidates.append((t, z))
    chosen: list[tuple[float, float]] = []
    for t, z in sorted(candidates, key=lambda c: (-c[1], c[0])):
        if all(abs(t - c) >= min_separation for c, _ in chosen):
            chosen.append((t, z))
        if len(chosen) >= top_n:
            break
    return chosen


def peak_windows(events: list[float], duration: float, *, window: float = 30.0,
                 step: float = 5.0, top_n: int = 8, margin: float = 8.0,
                 lead: float = 20.0, z_threshold: float = 1.5) -> list[Window]:
    """Fenêtres à transcrire autour des pics de densité.

    Chaque fenêtre est centrée sur la médiane des messages de la zone dense. Le
    chat réagit après l'action : la fenêtre remonte `lead` secondes avant ce
    centre et va `window / 2` après, plus la marge de chaque côté. Les
    fenêtres qui se chevauchent sont fusionnées.
    """
    ordered = sorted(events)
    series = density(ordered, duration, window=window, step=step)
    # Deux rafales proches donnent deux pics ; leurs fenêtres seront fusionnées.
    peaks = find_peaks(series, top_n=top_n, min_separation=window / 2, z_threshold=z_threshold)
    raw = []
    for t, z in peaks:
        inside = ordered[bisect.bisect_left(ordered, t):bisect.bisect_left(ordered, t + window)]
        center = inside[len(inside) // 2] if inside else t + window / 2
        raw.append(Window(max(0.0, center - lead - margin), min(duration, center + window / 2 + margin),
                          z, center))
    merged: list[Window] = []
    for w in sorted(raw, key=lambda w: w.start):
        if merged and w.start <= merged[-1].end:
            prev = merged[-1]
            best = prev if prev.score >= w.score else w
            merged[-1] = Window(prev.start, max(prev.end, w.end), max(prev.score, w.score), best.peak)
        else:
            merged.append(w)
    return sorted(merged, key=lambda w: -w.score)


# -- commentaires horodatés (YouTube) -----------------------------------------

_TIMESTAMP = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)(?![\d:])")


def comment_timestamps(comments: list[str], duration: float | None = None) -> list[float]:
    """Instants cités dans des commentaires (« 12:34 », « 1:02:03 »)."""
    out = []
    for text in comments:
        for h, m, s in _TIMESTAMP.findall(text):
            t = int(h or 0) * 3600 + int(m) * 60 + int(s)
            if duration is None or t <= duration:
                out.append(float(t))
    return out
