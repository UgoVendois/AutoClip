# AutoClip

Démon Linux qui transforme des vidéos longues en Shorts : transcription, choix des meilleurs extraits, recadrage 9:16 avec sous-titres mot à mot, validation manuelle, puis publication planifiée au compte-gouttes sur YouTube Shorts.

Phase 1 (MVP) : backend seul, piloté en ligne de commande ou par l'API locale. L'interface graphique arrive en phase 2.

## Installation

```sh
uv venv --python 3.12 .venv
uv pip install -e '.[dev]'
```

ffmpeg doit être installé sur le système.

## Essai sans publier

```sh
.venv/bin/autoclip dry-run ~/Vidéos/ma-video.mp4          # tout le circuit, publication simulée
.venv/bin/autoclip dry-run URL --stop-at-review            # s'arrête aux clips à valider
```

## Utilisation réelle

```sh
autoclip bot create "Mon bot"                       # dry run par défaut
autoclip credentials set youtube client_secret.json  # clés Google Cloud -> trousseau
autoclip account connect                             # connexion à la chaîne YouTube
autoclip account link 1 1                            # bot 1 -> compte 1
autoclip schedule 1 --every 4 --days mon,tue,wed,thu,fri --window 09:00-21:00
autoclip source 1 https://www.youtube.com/watch?v=...
autoclip daemon                                      # ou : systemctl --user start autoclip
autoclip review list                                 # clips à valider
autoclip review approve 3 4                          # validés -> publiés selon le planning
autoclip bot live 1                                  # passe le bot en publication réelle
```

Les vidéos envoyées par un projet Google Cloud non audité restent privées.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```
