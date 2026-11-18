"""Étape render : découpe, mise en page 9:16, coupe des silences, texte
d'accroche et sous-titres mot à mot incrustés."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ..jobs.queue import PermanentError
from . import layout as lay

WIDTH, HEIGHT = 1080, 1920
WHITE = "&H00FFFFFF"
YELLOW = "&H0000FFFF"  # format ASS : &HAABBGGRR


def _ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _clean(word: str) -> str:
    return word.strip().replace("\\", "/").replace("{", "(").replace("}", ")")


def _groups(words: list[dict], size: int) -> list[list[dict]]:
    """Paquets de `size` mots au plus, sans enjamber une fin de phrase."""
    groups: list[list[dict]] = []
    current: list[dict] = []
    for w in words:
        current.append(w)
        if len(current) >= size or w["word"].strip().endswith((".", "?", "!", "…")):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _wrap_hook(text: str, max_chars: int = 22) -> str:
    """Accroche sur deux lignes au plus, coupées entre les mots."""
    words, lines, line = text.split(), [], ""
    for w in words:
        if line and len(line) + 1 + len(w) > max_chars and len(lines) < 1:
            lines.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    lines.append(line)
    return "\\N".join(lines)


def build_ass(words: list[dict], clip_start: float, clip_end: float, *,
              group_size: int = 3, font: str = "DejaVu Sans", font_size: int = 84,
              subtitle_margin: int = 560, hook: str = "", hook_seconds: float = 3.0,
              hook_y: int = 360) -> str:
    """Sous-titres façon Shorts : quelques mots à l'écran, le mot prononcé en jaune.
    `hook` : texte d'accroche dans un cartouche, pendant les `hook_seconds` premières secondes."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,{font},{font_size},{WHITE},{WHITE},&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,2,80,80,{subtitle_margin},1
Style: Hook,{font},74,&H00141414,&H00141414,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,5,90,90,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    duration = clip_end - clip_start
    inside = [w for w in words if w["end"] > clip_start and w["start"] < clip_end]
    groups = _groups(inside, group_size)
    lines = []
    for gi, group in enumerate(groups):
        next_group_start = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else None
        for wi, word in enumerate(group):
            start = word["start"]
            if wi + 1 < len(group):
                end = group[wi + 1]["start"]
            elif next_group_start is not None and next_group_start - word["end"] < 0.4:
                end = next_group_start  # pas de clignotement entre deux paquets proches
            else:
                end = word["end"] + 0.25
            text = " ".join(
                f"{{\\c{YELLOW}&}}{_clean(w['word']).upper()}{{\\c{WHITE}&}}" if k == wi
                else _clean(w["word"]).upper()
                for k, w in enumerate(group)
            )
            rel_start = min(max(start - clip_start, 0.0), duration)
            rel_end = min(max(end - clip_start, 0.0), duration)
            if rel_end > rel_start:
                lines.append(f"Dialogue: 0,{_ts(rel_start)},{_ts(rel_end)},Word,,0,0,0,,{text}")
    hook = _clean(hook or "")
    if hook and hook_seconds > 0:
        # Cartouche blanc (BorderStyle 3), texte sombre, léger fondu.
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(min(hook_seconds, duration))},Hook,,0,0,0,,"
                     f"{{\\pos({WIDTH // 2},{hook_y})\\fad(120,250)}}{_wrap_hook(hook)}")
    return header + "\n".join(lines) + "\n"


# -- coupe des silences ------------------------------------------------------------------

_SILENCE = re.compile(r"silence_(start|end): (-?[\d.]+)")


def silences(media: Path, start: float, end: float, *, noise_db: float = -35, min_dur: float = 0.7) -> list[tuple[float, float]]:
    """Passages sans son (ni voix, ni jeu) dans [start, end], en temps relatif au clip."""
    proc = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                           "-i", str(media), "-vn", "-af", f"silencedetect=n={noise_db}dB:d={min_dur}",
                           "-f", "null", "-"], capture_output=True, text=True)
    out, current = [], None
    for kind, value in _SILENCE.findall(proc.stderr):
        if kind == "start":
            current = max(0.0, float(value))
        elif current is not None:
            out.append((current, float(value)))
            current = None
    if current is not None:
        out.append((current, end - start))
    return out


def keep_intervals(duration: float, quiet: list[tuple[float, float]], words: list[dict], *,
                   pad: float = 0.15, min_cut: float = 0.4) -> list[tuple[float, float]]:
    """Parties gardées : tout sauf les silences (raccourcis de `pad` de chaque côté
    pour ne pas couper sec), jamais pendant un mot. Temps relatifs au clip."""
    cuts = []
    for a, b in quiet:
        a, b = a + pad, b - pad
        for w in words:  # un mot chuchoté reste
            if w["end"] + pad > a and w["start"] - pad < b:
                if w["start"] - pad - a >= min_cut:
                    cuts.append((a, w["start"] - pad))
                a = max(a, w["end"] + pad)
        if b - a >= min_cut:
            cuts.append((a, b))
    keep, t = [], 0.0
    for a, b in sorted(cuts):
        if a > t:
            keep.append((t, a))
        t = max(t, b)
    if t < duration:
        keep.append((t, duration))
    return [(round(a, 3), round(b, 3)) for a, b in keep if b - a > 0.05] or [(0.0, duration)]


def remap_words(words: list[dict], keep: list[tuple[float, float]]) -> list[dict]:
    """Temps des mots (relatifs au clip) -> temps dans la vidéo raccourcie."""
    out, offset = [], 0.0
    for a, b in keep:
        for w in words:
            if a <= w["start"] < b:
                out.append({**w, "start": w["start"] - a + offset, "end": min(w["end"], b) - a + offset})
        offset += b - a
    return out


# -- rendu -------------------------------------------------------------------------------

def render_clip(media: Path, start: float, end: float, words: list[dict], out: Path, *,
                group_size: int = 3, font: str = "DejaVu Sans", font_size: int = 84,
                layout: str = "crop", facecam: "lay.Box | None" = None, trim_silences: bool = False,
                hook: str = "", hook_seconds: float = 3.0) -> Path:
    """Produit `out` (MP4 1080x1920). Ne refait rien si le fichier existe déjà.

    `layout` : facecam (avec `facecam`), fit ou crop. `words` : temps de la vidéo source."""
    if out.exists():
        return out
    if not Path(media).is_file():
        raise PermanentError(f"vidéo source introuvable : {media}")
    out.parent.mkdir(parents=True, exist_ok=True)
    src_w, src_h, has_audio = lay.probe(Path(media))
    duration = end - start
    rel_words = [{**w, "start": w["start"] - start, "end": w["end"] - start}
                 for w in words if w["end"] > start and w["start"] < end]
    keep = [(0.0, duration)]
    if trim_silences and has_audio:
        keep = keep_intervals(duration, silences(Path(media), start, end), rel_words)
    new_duration = sum(b - a for a, b in keep)
    placement = lay.PLACEMENTS.get(layout, lay.PLACEMENTS["crop"])

    ass = out.with_suffix(".ass")
    ass.write_text(build_ass(remap_words(rel_words, keep), 0.0, new_duration, group_size=group_size, font=font,
                             font_size=font_size, subtitle_margin=placement.subtitle_margin, hook=hook,
                             hook_seconds=hook_seconds, hook_y=placement.hook_y), encoding="utf-8")

    # Morceaux gardés -> [vcat] (+ [acat]), puis mise en page, puis sous-titres.
    parts = []
    for i, (a, b) in enumerate(keep):
        parts.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{i}]")
        if has_audio:
            parts.append(f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[a{i}]")
    n = len(keep)
    if has_audio:
        parts.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vcat][acat]")
    else:
        parts.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vcat]")
    parts.append(lay.video_filter(layout, facecam, src_w, src_h, "vcat", "vlay"))
    parts.append(f"[vlay]ass={ass.name}[vout]")

    tmp = out.with_name(out.stem + ".tmp.mp4")
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(Path(media).resolve()),
        "-filter_complex", ";".join(parts), "-map", "[vout]",
        *(["-map", "[acat]", "-c:a", "aac", "-b:a", "128k", "-ar", "48000"] if has_audio else ["-an"]),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(tmp.name),
    ]
    # cwd = dossier de sortie : le chemin du .ass n'a pas besoin d'échappement.
    proc = subprocess.run(cmd, cwd=out.parent, capture_output=True, text=True)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg a échoué : {proc.stderr.strip()[-2000:]}")
    os.replace(tmp, out)
    return out
