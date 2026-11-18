"""Mise en page 9:16 des clips de stream.

- facecam : la webcam du streamer en haut (1080x608), le jeu en dessous (1080x1312) ;
- fit : l'image entière au centre, sur un fond flouté de la même vidéo (rien n'est rogné) ;
- crop : recadrage central (contenu filmé en plein cadre).

« auto » cherche une facecam : un visage détecté (modèle YuNet, local) toujours au
même endroit sur plusieurs images. Un visage de personnage de jeu bouge, une webcam
incrustée non. Sans facecam, « auto » choisit fit.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

# Avant tout import de cv2 : pas d'avertissements du moteur DNN dans la console.
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

# Avant tout import de cv2 : pas d'avertissements du moteur DNN dans les journaux.
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

WIDTH, HEIGHT = 1080, 1920
CAM_H = 608  # 16:9 sur toute la largeur
GAME_H = HEIGHT - CAM_H
MODEL = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
LAYOUTS = ("auto", "facecam", "fit", "crop")


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def iou(self, other: "Box") -> float:
        ix = max(0, min(self.x + self.w, other.x + other.w) - max(self.x, other.x))
        iy = max(0, min(self.y + self.h, other.y + other.h) - max(self.y, other.y))
        inter = ix * iy
        union = self.w * self.h + other.w * other.h - inter
        return inter / union if union else 0.0


@dataclass(frozen=True)
class Placement:
    """Où poser le texte d'accroche et les sous-titres selon la mise en page."""

    hook_y: int  # centre vertical du texte d'accroche
    subtitle_margin: int  # marge basse des sous-titres (ASS MarginV)


PLACEMENTS = {
    "facecam": Placement(hook_y=CAM_H, subtitle_margin=560),  # accroche à la jonction cam/jeu
    "fit": Placement(hook_y=470, subtitle_margin=560),  # au-dessus de l'image du jeu
    "crop": Placement(hook_y=360, subtitle_margin=560),
}


# -- détection --------------------------------------------------------------------

def probe(media: Path) -> tuple[int, int, bool]:
    """Largeur, hauteur, présence d'une piste audio."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                          "-of", "json", str(media)], capture_output=True, text=True)
    streams = json.loads(out.stdout or "{}").get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError(f"aucune piste vidéo dans {media}")
    return int(video["width"]), int(video["height"]), any(s.get("codec_type") == "audio" for s in streams)


def sample_frames(media: Path, start: float, end: float, n: int = 10, width: int = 640) -> list:
    """`n` images réparties sur [start, end], réduites à `width` px de large."""
    import cv2

    frames = []
    with tempfile.TemporaryDirectory() as tmp:
        step = max(end - start, 0.1) / (n + 1)
        for i in range(n):
            t = start + step * (i + 1)
            path = Path(tmp) / f"{i}.png"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(media),
                            "-frames:v", "1", "-vf", f"scale={width}:-2", str(path)], capture_output=True)
            if path.exists():
                frames.append(cv2.imread(str(path)))
    return [f for f in frames if f is not None]


def yunet_faces(frame) -> list[Box]:
    import cv2

    h, w = frame.shape[:2]
    detector = cv2.FaceDetectorYN.create(str(MODEL), "", (w, h), score_threshold=0.75)
    _, faces = detector.detect(frame)
    if faces is None:
        return []
    return [Box(int(f[0]), int(f[1]), int(f[2]), int(f[3])) for f in faces]


def stable_face(per_frame: list[list[Box]], frame_w: int, *, min_share: float = 0.6,
                max_face_ratio: float = 0.3) -> Box | None:
    """Visage présent au même endroit sur au moins `min_share` des images.

    Les visages trop grands (plus de `max_face_ratio` de la largeur) sont ignorés :
    c'est une caméra plein cadre, pas une webcam incrustée."""
    if not per_frame:
        return None
    needed = max(2, int(len(per_frame) * min_share + 0.999))
    best: list[Box] = []
    for frame in per_frame:
        for face in frame:
            if face.w > frame_w * max_face_ratio:
                continue
            matches = [next((f for f in other if f.iou(face) > 0.4), None) for other in per_frame]
            matched = [m for m in matches if m is not None]
            if len(matched) > len(best):
                best = matched
    if len(best) < needed:
        return None
    med = lambda vals: sorted(vals)[len(vals) // 2]  # noqa: E731
    return Box(med([b.x for b in best]), med([b.y for b in best]), med([b.w for b in best]), med([b.h for b in best]))


def facecam_region(face: Box, frame_w: int, frame_h: int, *, zoom: float = 3.4) -> Box:
    """Rectangle 16:9 autour du visage (la webcam entière, épaules comprises)."""
    w = min(frame_w, face.w * zoom)
    h = w * 9 / 16
    if h > frame_h:
        h = frame_h
        w = h * 16 / 9
    cx, cy = face.cx, face.cy + h * 0.12  # le visage est dans le haut de la webcam
    x = min(max(cx - w / 2, 0), frame_w - w)
    y = min(max(cy - h / 2, 0), frame_h - h)
    even = lambda v: int(v) // 2 * 2  # noqa: E731
    return Box(even(x), even(y), even(w), even(h))


def detect_facecam(media: Path, start: float, end: float, *,
                   detector: Callable[[object], list[Box]] = yunet_faces) -> Box | None:
    """Zone de la webcam en pixels de la vidéo source, ou None."""
    src_w, src_h, _ = probe(media)
    frames = sample_frames(media, start, end)
    if not frames:
        return None
    scale = src_w / frames[0].shape[1]
    face = stable_face([detector(f) for f in frames], frames[0].shape[1])
    if face is None:
        return None
    face = Box(int(face.x * scale), int(face.y * scale), int(face.w * scale), int(face.h * scale))
    return facecam_region(face, src_w, src_h)


def choose(layout: str, media: Path, start: float, end: float, cache_file: Path | None = None,
           detector: Callable[[object], list[Box]] = yunet_faces) -> tuple[str, Box | None]:
    """Mise en page effective ("facecam" | "fit" | "crop") et zone de la webcam."""
    if layout in ("fit", "crop"):
        return layout, None
    if cache_file and cache_file.exists():
        data = json.loads(cache_file.read_text())
        cam = Box(**data["facecam"]) if data["facecam"] else None
    else:
        cam = detect_facecam(media, start, end, detector=detector)
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"facecam": asdict(cam) if cam else None}))
    if cam is not None:
        return "facecam", cam
    return "fit", None  # auto ou facecam demandée sans webcam visible


# -- filtres ffmpeg -----------------------------------------------------------------

def video_filter(layout: str, cam: Box | None, src_w: int, src_h: int, inp: str, out: str) -> str:
    """Graphe ffmpeg de `inp` (vidéo source) vers `out` (1080x1920)."""
    if layout == "facecam" and cam is not None:
        game_w = min(src_w, int(src_h * WIDTH / GAME_H)) // 2 * 2
        game_x = (src_w - game_w) // 2
        return (f"[{inp}]split=2[cam_in][game_in];"
                f"[cam_in]crop={cam.w}:{cam.h}:{cam.x}:{cam.y},scale={WIDTH}:{CAM_H}:flags=lanczos,setsar=1[cam];"
                f"[game_in]crop={game_w}:{src_h}:{game_x}:0,scale={WIDTH}:{GAME_H}:flags=lanczos,setsar=1[game];"
                f"[cam][game]vstack=inputs=2[{out}]")
    if layout == "fit":
        return (f"[{inp}]split=2[bg_in][fg_in];"
                f"[bg_in]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},"
                f"boxblur=24:2,eq=brightness=-0.08[bg];"
                f"[fg_in]scale={WIDTH}:-2:flags=lanczos[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[{out}]")
    return (f"[{inp}]crop=w='trunc(min(iw,ih*9/16)/2)*2':h='trunc(min(ih,iw*16/9)/2)*2',"
            f"scale={WIDTH}:{HEIGHT}:flags=lanczos,setsar=1[{out}]")


def keyframe(media: Path, t: float, width: int = 512) -> bytes | None:
    """Une image JPEG à l'instant `t` (secondes dans le fichier), pour l'IA."""
    proc = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(t, 0):.2f}", "-i", str(media),
                           "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "image2pipe", "-c:v", "mjpeg",
                           "-q:v", "6", "-"], capture_output=True)
    return proc.stdout if proc.returncode == 0 and proc.stdout else None
