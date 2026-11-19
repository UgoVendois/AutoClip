"""Étape transcribe : transcription mot à mot avec faster-whisper (CPU, int8)."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Protocol

from ..jobs.queue import PermanentError

Word = dict  # {"start": float, "end": float, "word": str}


class Transcriber(Protocol):
    def __call__(self, media: Path, model: str, language: str | None) -> dict: ...


SAMPLE_RATE = 16000  # fréquence attendue par Whisper


def decode_audio(media: Path):
    """Audio mono 16 kHz en float32, décodé par ffmpeg.

    faster-whisper décode d'habitude avec PyAV, mais son appel n'est plus compatible
    avec PyAV 15+ (`metadata_errors`) : ffmpeg, déjà requis, fait le travail."""
    import numpy as np

    proc = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(media), "-vn", "-ac", "1",
                           "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg (audio) : {proc.stderr.decode(errors='replace').strip()[-300:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


class FasterWhisperTranscriber:
    """Garde les modèles chargés en mémoire entre deux vidéos."""

    def __init__(self, device: str = "auto", compute_type: str = "int8") -> None:
        self.device = device
        self.compute_type = compute_type
        self._models: dict[str, object] = {}
        self._lock = threading.Lock()

    def _model(self, name: str):
        with self._lock:
            if name not in self._models:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:  # pragma: no cover
                    raise PermanentError("faster-whisper n'est pas installé") from exc
                self._models[name] = WhisperModel(name, device=self.device,
                                                  compute_type=self.compute_type)
            return self._models[name]

    def __call__(self, media: Path, model: str, language: str | None) -> dict:
        segments, info = self._model(model).transcribe(
            decode_audio(media),
            language=language,
            word_timestamps=True,
            vad_filter=True,
            beam_size=5,
        )
        words: list[Word] = []
        for segment in segments:
            for w in segment.words or []:
                words.append({"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word})
        return {"language": info.language, "duration": info.duration, "words": words}
