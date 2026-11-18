"""Étape ingest : récupère la vidéo source (URL via yt-dlp ou fichier local)."""

from __future__ import annotations

from pathlib import Path

from ..jobs.queue import PermanentError


def resolve_local(uri: str) -> Path:
    path = Path(uri).expanduser().resolve()
    if not path.is_file():
        raise PermanentError(f"fichier introuvable : {path}")
    return path


def download(url: str, directory: Path) -> Path:
    """Télécharge dans `directory`. Si la vidéo y est déjà, ne retélécharge pas."""
    existing = sorted(directory.glob("video.*"))
    existing = [p for p in existing if p.suffix not in (".part", ".ytdl")]
    if existing:
        return existing[0]
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise PermanentError("yt-dlp n'est pas installé dans l'environnement") from exc

    opts = {
        "outtmpl": str(directory / "video.%(ext)s"),
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as exc:
            message = str(exc)
            # Vidéo privée, supprimée, URL invalide : inutile de réessayer.
            if any(s in message for s in ("Unsupported URL", "Private video", "Video unavailable",
                                          "This video is not available", "404")):
                raise PermanentError(message) from exc
            raise
    path = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
    if not path.exists():
        candidates = [p for p in directory.glob("video.*") if p.suffix not in (".part", ".ytdl")]
        if not candidates:
            raise RuntimeError("yt-dlp n'a produit aucun fichier")
        path = candidates[0]
    return path
