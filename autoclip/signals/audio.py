"""Énergie audio (sonie à court terme, EBU R128) : signal de secours sans chat."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .peaks import Window, find_peaks

_LINE = re.compile(r"t:\s*([\d.]+).*?S:\s*(-?[\d.]+|-inf)")


def loudness_series(media: Path, step: float = 1.0) -> list[tuple[float, float]]:
    """Sonie à court terme (LUFS, fenêtre de 3 s) échantillonnée chaque `step` s."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(media), "-vn",
         "-af", "ebur128=framelog=verbose", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg ebur128 : {proc.stderr.strip()[-500:]}")
    series: list[tuple[float, float]] = []
    next_t = 0.0
    for match in _LINE.finditer(proc.stderr):
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
