"""Fenêtre de bureau (pywebview) autour de l'interface web.

Si le démon tourne déjà (service systemd), la fenêtre s'y connecte. Sinon elle
démarre un démon intégré qui s'arrête à la fermeture de la fenêtre.
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.request
from pathlib import Path

from .config import API_HOST, API_PORT

log = logging.getLogger(__name__)

VIDEO_TYPES = ("Vidéos (*.mp4;*.mkv;*.mov;*.webm;*.avi)", "Tous les fichiers (*.*)")


def daemon_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=1) as resp:
            return json.load(resp).get("ok") is True
    except (OSError, ValueError):
        return False


class JsBridge:
    """Fonctions appelables depuis l'interface : `window.pywebview.api.<nom>()`."""

    def __init__(self) -> None:
        self.window = None

    def pick_video(self) -> str | None:
        import webview

        result = self.window.create_file_dialog(webview.FileDialog.OPEN, file_types=VIDEO_TYPES)
        return result[0] if result else None

    def pick_json(self) -> str | None:
        """Lit un fichier JSON choisi par l'utilisateur (client_secret.json)."""
        import webview

        result = self.window.create_file_dialog(webview.FileDialog.OPEN,
                                                file_types=("JSON (*.json)",))
        return Path(result[0]).read_text() if result else None


def run_app(home: Path | None = None, host: str = API_HOST, port: int = API_PORT,
            debug: bool = False) -> int:
    import webview

    url = f"http://{host}:{port}"
    daemon = None
    if daemon_running(url):
        log.info("démon déjà actif sur %s", url)
    else:
        from .runtime import Daemon

        daemon = Daemon(home, host, port)
        try:
            daemon.start_in_thread()
        except RuntimeError as exc:
            print(f"erreur : {exc}", file=sys.stderr)
            return 1

    bridge = JsBridge()
    bridge.window = webview.create_window("AutoClip", url, js_api=bridge, width=1320, height=860,
                                          min_size=(960, 620), background_color="#1f1e1d")
    try:
        webview.start(gui="gtk", debug=debug)
    finally:
        if daemon is not None:
            daemon.shutdown()
    return 0


DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=AutoClip
Comment=Clipping et publication de Shorts
Exec={exe} app
Icon=video-x-generic
Terminal=false
Categories=AudioVideo;Video;
"""


def install_desktop_entry() -> Path:
    """Ajoute AutoClip au lanceur d'applications de l'utilisateur."""
    exe = Path(sys.argv[0]).resolve()
    target = Path.home() / ".local/share/applications/autoclip.desktop"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(DESKTOP_ENTRY.format(exe=exe))
    return target
