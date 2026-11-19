"""Énergie audio (sonie à court terme, EBU R128) : signal de secours sans chat."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from ..jobs.queue import Cancelled

from .peaks import Window, find_peaks

_LINE = re.compile(r"t:\s*([\d.]+).*?S:\s*(-?[\d.]+|-inf)")


def loudness_series(media: Path, step: float = 1.0,
                    should_stop: Callable[[], bool] | None = None) -> list[tuple[float, float]]:
    """Sonie à court terme (LUFS, fenêtre de 3 s) échantillonnée chaque `step` s.

    `should_stop` est consulté chaque seconde : s'il répond vrai, ffmpeg est arrêté."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(media), "-vn",
           # framelog=info : avec « verbose », ffmpeg 7+ masque les mesures au niveau de log par défaut.
           "-af", "ebur128=framelog=info", "-f", "null", "-"]
    # La sortie d'un live de plusieurs heures dépasse la taille d'un tube : fichier temporaire.
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log)
        try:
            while proc.poll() is None:
                if should_stop is not None and should_stop():
                    raise Cancelled("analyse du son annulée")
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        log.seek(0)
        stderr = log.read()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg ebur128 : {stderr.strip()[-500:]}")
    series: list[tuple[float, float]] = []
    next_t = 0.0
    for match in _LINE.finditer(stderr):
        t, s = float(match.group(1)), match.group(2)
        if t + 1e-6 >= next_t:
            series.append((t, -70.0 if s == "-inf" else max(float(s), -70.0)))
            next_t = t + step
    return series


def energy_windows(series: list[tuple[float, float]], duration: float, *, window: float = 30.0,
                   top_n: int = 8, margin: float = 8.0, z_threshold: float = 1.0) -> list[Window]:
    """Fenêtres où la sonie moyenne sur `window` secondes est la plus forte."""
    if not series:
        return []
    smoothed = []
    values = [v for _, v in series]
    times = [t for t, _ in series]
    span = max(1, int(round(window / max(times[1] - times[0], 1e-3)))) if len(times) > 1 else 1
    for i in range(0, max(len(values) - span + 1, 1), max(span // 6, 1)):
        chunk = values[i:i + span]
        smoothed.append((times[i], sum(chunk) / len(chunk)))
    peaks = find_peaks(smoothed, top_n=top_n, min_separation=window, z_threshold=z_threshold)
    return [Window(max(0.0, t - margin), min(duration, t + window + margin), z, t + window / 2)
            for t, z in sorted(peaks, key=lambda p: -p[1])]
