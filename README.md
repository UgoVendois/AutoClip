# AutoClip

Démon Linux qui transforme des vidéos longues en Shorts : transcription, choix des meilleurs extraits, recadrage 9:16 avec sous-titres mot à mot, validation manuelle, puis publication planifiée au compte-gouttes sur YouTube Shorts.

Un démon Python fait le travail ; une fenêtre de bureau (Svelte dans pywebview) le pilote : bots, validation des clips, planning, statistiques, clés d'API.

## Installation

Prérequis système : ffmpeg, Node.js + npm (pour construire l'interface), WebKitGTK (`gir1.2-webkit2-4.1`, présent sur Ubuntu).

```sh
uv venv --system-site-packages .venv      # accès à PyGObject (GTK) du système
uv pip install -e '.[dev]'
npm --prefix frontend install && npm --prefix frontend run build
.venv/bin/autoclip install-desktop        # AutoClip dans le lanceur d'applications
```

## Interface

```sh
.venv/bin/autoclip app        # ouvre la fenêtre (démarre le démon si besoin)
```

Développement de l'interface avec rechargement à chaud : lancer `autoclip daemon`, puis `npm --prefix frontend run dev`.

## Essai sans publier

```sh
.venv/bin/autoclip dry-run ~/Vidéos/ma-video.mp4          # tout le circuit, publication simulée
.venv/bin/autoclip dry-run URL --stop-at-review            # s'arrête aux clips à valider
```

## En ligne de commande

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

## Découverte automatique (YouTube, Twitch) et IA

Un bot peut surveiller des chaînes et trouver seul les moments à clipper, en dépensant un minimum de tokens :

1. **Découverte** : flux RSS des chaînes YouTube, API Helix pour les VODs Twitch. Chaque contenu n'est traité qu'une fois.
2. **Tri sans IA** : densité du chat (fenêtre glissante de 30 s) et détection des pics. Pour YouTube, les horodatages cités en commentaire ; en dernier recours, l'énergie audio.
3. **Seules les fenêtres autour des pics** sont téléchargées, transcrites et envoyées à l'IA, en un seul appel par vidéo (score, raison, titre, description en JSON validé).
4. **Budget** de tokens par bot et par jour : au-delà, l'analyse est reportée au lendemain et une alerte est créée. Réponses mises en cache.

Twitch n'offre pas d'API officielle pour le chat des VODs : le démon enregistre le chat pendant les lives (EventSub), avec un compte Twitch quelconque.

```sh
autoclip credentials set twitch                 # Client ID / Secret d'une app dev.twitch.tv
autoclip twitch-login                           # code à saisir sur twitch.tv/activate
autoclip credentials set anthropic              # clé d'API Claude
autoclip credentials set youtube_data           # facultatif : commentaires YouTube
autoclip watch add 1 twitch @streamer --every 30
autoclip watch add 1 youtube @chaine --every 120
autoclip ai set 1 provider=anthropic daily_token_budget=150000
autoclip alerts
```

Fournisseurs : `anthropic` (Claude, `claude-opus-5-5` par défaut, réglable avec `model=`), `ollama` (local), `fake` (tests). Un bot en dry run utilise toujours le faux fournisseur : aucun appel payant.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```
