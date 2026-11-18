"""Génération d'images (photo de profil, bannière) et mise au format avec ffmpeg.

Fournisseurs : Gemini (« Nano Banana »), OpenAI (GPT Image), ou « svg » : un
modèle de texte dessine un SVG vectoriel, rendu ensuite par ffmpeg (librsvg).
Le SVG marche avec n'importe quel fournisseur de texte, sans modèle d'images.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel

from .base import AIProvider, AIProviderError
from .remote import GEMINI_URL, http_json

IMAGE_PROVIDERS = ("svg", "gemini", "openai", "fake")
DEFAULT_IMAGE_MODELS = {
    "gemini": "gemini-3-pro-image",
    "openai": "gpt-image-2.6-flare",
}

# Formats finaux exigés par YouTube.
FORMATS = {
    "avatar": (800, 800, "png"),  # photo de profil, affichée en cercle
    "banner": (2560, 1440, "jpg"),  # bannière : 2048x1152 minimum, 6 Mo maximum
}
ASPECT = {"avatar": "1:1", "banner": "16:9"}


class GeneratedImage:
    def __init__(self, data: bytes, ext: str) -> None:
        self.data = data
        self.ext = ext  # png | jpg | webp | svg


class ImageGenerator(ABC):
    name: str
    model: str

    @abstractmethod
    def generate(self, prompt: str, kind: str) -> GeneratedImage:
        """`kind` : avatar (carré) ou banner (16:9)."""


def sniff_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"\xff\xd8"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.lstrip()[:1] == b"<":
        return "svg"
    raise RuntimeError("format d'image inconnu renvoyé par le modèle")


class GeminiImage(ImageGenerator):
    name = "gemini"

    def __init__(self, api_key: str, model: str | None = None, base_url: str = GEMINI_URL) -> None:
        self.key = api_key
        self.model = model or DEFAULT_IMAGE_MODELS["gemini"]
        self.base_url = base_url

    def generate(self, prompt: str, kind: str) -> GeneratedImage:
        image_config = {"aspectRatio": ASPECT[kind]}
        if not self.model.startswith("gemini-2."):  # les anciens modèles n'ont pas imageSize
            image_config["imageSize"] = "2K"
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": image_config},
        }
        data = http_json("POST", f"{self.base_url}/models/{self.model}:generateContent",
                         headers={"x-goog-api-key": self.key}, body=body, provider="Gemini")
        for cand in data.get("candidates") or []:
            for part in cand.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    raw = base64.b64decode(inline["data"])
                    return GeneratedImage(raw, sniff_ext(raw))
        reason = (data.get("candidates") or [{}])[0].get("finishReason") or \
            data.get("promptFeedback", {}).get("blockReason")
        raise AIProviderError(f"Gemini n'a renvoyé aucune image ({reason or 'raison inconnue'})")


class OpenAIImage(ImageGenerator):
    name = "openai"
    base_url = "https://api.openai.com/v1"

    def __init__(self, api_key: str, model: str | None = None, base_url: str | None = None) -> None:
        self.key = api_key
        self.model = model or DEFAULT_IMAGE_MODELS["openai"]
        if base_url:
            self.base_url = base_url

    def _size(self, kind: str) -> str:
        if kind == "avatar":
            return "1024x1024"
        # Tailles libres (multiples de 16) seulement sur les modèles récents.
        legacy = self.model.startswith(("gpt-image-1", "dall-e"))
        return "1536x1024" if legacy else "2048x1152"

    def generate(self, prompt: str, kind: str) -> GeneratedImage:
        body = {"model": self.model, "prompt": prompt, "size": self._size(kind), "n": 1}
        data = http_json("POST", f"{self.base_url}/images/generations",
                         headers={"Authorization": f"Bearer {self.key}"}, body=body, provider="OpenAI")
        items = data.get("data") or []
        if not items or not items[0].get("b64_json"):
            raise AIProviderError("OpenAI n'a renvoyé aucune image")
        raw = base64.b64decode(items[0]["b64_json"])
        return GeneratedImage(raw, sniff_ext(raw))


# -- SVG dessiné par un modèle de texte ------------------------------------------------

SVG_SYSTEM = """Tu es directeur artistique. Tu dessines en SVG des visuels de chaîne YouTube : nets, modernes, lisibles en petit.

Contraintes techniques strictes :
- un seul document SVG autonome, avec xmlns, width, height et viewBox aux dimensions demandées ;
- aucune image externe, aucun lien, aucun script, aucune police téléchargée : seulement des formes, dégradés, filtres simples et les polices génériques sans-serif ou serif ;
- tout le texte important reste dans la zone sûre indiquée.

Renvoie le SVG complet dans le champ svg."""


class SvgArt(BaseModel):
    svg: str

    @classmethod
    def fake(cls, user: str) -> "SvgArt":
        req = json.loads(user)
        w, h = req["width"], req["height"]
        c0, c1 = ([*(req.get("palette") or []), "#D97757", "#2B2A27"])[:2]
        name = req.get("name", "")
        if w == h:  # photo de profil : l'initiale seule, lisible en petit
            name = name[:1].upper()
        name = _xml_escape(name)
        size = int(min(w, h) * (0.12 if w > h else 0.5))
        return cls(svg=(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{c0}"/><stop offset="1" stop-color="{c1}"/></linearGradient></defs>'
            f'<rect width="{w}" height="{h}" fill="url(#g)"/>'
            f'<text x="{w / 2}" y="{h / 2}" font-family="sans-serif" font-size="{size}" font-weight="700" '
            f'fill="#fff" text-anchor="middle" dominant-baseline="middle">{name}</text></svg>'))


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_UNSAFE_SVG = re.compile(r"<\s*script|<!ENTITY|<!DOCTYPE|<\s*foreignObject|"
                         r"(?:href|src)\s*=\s*[\"'](?!#|data:image/(?:png|jpeg);base64,)|url\(\s*[\"']?(?!#)",
                         re.IGNORECASE)


def check_svg(svg: str) -> str:
    """Refuse tout SVG qui pourrait lire un fichier local ou le réseau au rendu."""
    if _UNSAFE_SVG.search(svg):
        raise RuntimeError("SVG refusé : référence externe, script ou entité")
    if "<svg" not in svg:
        raise RuntimeError("SVG invalide renvoyé par le modèle")
    return svg


class SvgImage(ImageGenerator):
    name = "svg"

    def __init__(self, text: AIProvider) -> None:
        self.text = text
        self.model = text.model
        self.context: dict = {}  # nom, palette : fournis par l'agent identité

    def generate(self, prompt: str, kind: str) -> GeneratedImage:
        width, height, _ = FORMATS[kind]
        safe = ("le cercle central (le coin sera rogné en rond)" if kind == "avatar"
                else "la bande centrale de 1546 x 423 px (le reste est rogné sur mobile)")
        user = json.dumps({**self.context, "kind": kind, "width": width, "height": height,
                           "safe_zone": safe, "brief": prompt}, ensure_ascii=False)
        art, _usage = self.text.complete_json(SVG_SYSTEM, user, SvgArt, max_tokens=16000)
        return GeneratedImage(check_svg(art.svg).encode(), "svg")


class FakeImage(SvgImage):
    """Aucun appel : un dégradé et le nom, rendus par le vrai chemin SVG -> ffmpeg."""

    name = "fake"

    def __init__(self) -> None:
        self.model = "fake-image"
        self.context = {}
        self.calls: list[tuple[str, str]] = []

    def generate(self, prompt: str, kind: str) -> GeneratedImage:
        self.calls.append((prompt, kind))
        width, height, _ = FORMATS[kind]
        art = SvgArt.fake(json.dumps({**self.context, "width": width, "height": height}))
        return GeneratedImage(check_svg(art.svg).encode(), "svg")


def make_image_generator(provider: str, model: str | None, text: AIProvider | None) -> ImageGenerator:
    from .providers import api_key

    if provider == "fake":
        return FakeImage()
    if provider == "gemini":
        return GeminiImage(api_key("gemini"), model)
    if provider == "openai":
        return OpenAIImage(api_key("openai"), model)
    if provider == "svg":
        if text is None:
            raise AIProviderError("le mode SVG demande un fournisseur de texte")
        return SvgImage(text)
    raise AIProviderError(f"générateur d'images inconnu : {provider!r}")


def finalize(image: GeneratedImage, out: Path, kind: str) -> Path:
    """Recadre et redimensionne au format YouTube (ffmpeg, rendu SVG par librsvg)."""
    width, height, _ = FORMATS[kind]
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"source.{image.ext}"
        src.write_bytes(image.data)
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), "-vf",
               f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
               f"crop={width}:{height}", "-frames:v", "1"]
        if out.suffix == ".jpg":
            cmd += ["-q:v", "2"]
        proc = subprocess.run([*cmd, str(out)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"mise au format de l'image impossible : {proc.stderr.strip()[-300:]}")
    return out
