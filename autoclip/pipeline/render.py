"""Étape render : découpe, recadrage 9:16 et sous-titres mot à mot incrustés."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..jobs.queue import PermanentError

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


def build_ass(words: list[dict], clip_start: float, clip_end: float, *,
              group_size: int = 3, font: str = "DejaVu Sans", font_size: int = 84) -> str:
    """Sous-titres façon Shorts : quelques mots à l'écran, le mot prononcé en jaune."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,{font},{font_size},{WHITE},{WHITE},&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,2,80,80,560,1

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
    return header + "\n".join(lines) + "\n"


def render_clip(media: Path, start: float, end: float, words: list[dict], out: Path, *,
                group_size: int = 3, font: str = "DejaVu Sans", font_size: int = 84) -> Path:
    """Produit `out` (MP4 1080x1920). Ne refait rien si le fichier existe déjà."""
    if out.exists():
        return out
    if not Path(media).is_file():
        raise PermanentError(f"vidéo source introuvable : {media}")
    out.parent.mkdir(parents=True, exist_ok=True)
    ass = out.with_suffix(".ass")
    ass.write_text(build_ass(words, start, end, group_size=group_size, font=font,
                             font_size=font_size), encoding="utf-8")
    tmp = out.with_name(out.stem + ".tmp.mp4")
    # Recadrage central au format 9:16 (largeur et hauteur paires), puis 1080x1920.
    vf = (
        "crop=w='trunc(min(iw,ih*9/16)/2)*2':h='trunc(min(ih,iw*16/9)/2)*2',"
        f"scale={WIDTH}:{HEIGHT}:flags=lanczos,setsar=1,"
        f"ass={ass.name}"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(Path(media).resolve()), "-t", f"{end - start:.3f}",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
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
