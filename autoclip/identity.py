"""Agent identité : propose l'habillage d'une chaîne (noms, description,
mots-clés, palette, photo, bannière, playlists), puis l'applique sur YouTube
après validation.

Déroulé (jobs) : identity_generate -> identity_images -> [validation dans l'UI]
-> identity_apply. Ensuite, chaque Short publié passe par playlist_add.

Le nom et la photo de profil ne sont pas modifiables par l'API YouTube : ils
sont proposés, l'utilisateur les reporte dans YouTube Studio.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import services
from .ai.base import AIProvider
from .db import tx
from .jobs.queue import JobQueue, PermanentError
from .jobs.worker import Task
from .publishers import Account
from .publishers.channel import ChannelManager, DryRunChannelManager, channel_manager_for

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Tu es stratège de marque pour des chaînes YouTube de Shorts : des extraits courts tirés de streams Twitch et de vidéos YouTube, publiés automatiquement.

À partir du brief et du contexte (chaînes suivies, titres des derniers clips), propose l'identité complète d'une chaîne :
- names : 3 noms de chaîne courts (30 caractères maximum), mémorables, faciles à prononcer, distincts entre eux. Évite d'usurper le nom exact d'un créateur suivi : la chaîne publie ses extraits mais n'est pas lui.
- tagline : une accroche de 6 à 10 mots.
- description : description de la chaîne en français, 400 à 900 caractères, qui dit ce qu'on y trouve, le rythme de publication et crédite les créateurs d'origine ; pas de hashtags en rafale.
- keywords : 8 à 15 mots-clés de recherche (expressions courtes).
- palette : 3 couleurs hexadécimales (#RRGGBB) : fond, accent, texte, avec un bon contraste.
- avatar_prompt : description visuelle en anglais d'un logo pour la photo de profil : forme simple et centrée, lisible à 48 px et rognée en cercle, sans petit texte (au plus une initiale).
- banner_prompt : description visuelle en anglais de la bannière 16:9 : le nom de la chaîne et l'accroche au centre, dans la bande centrale (le reste est décoratif et rogné sur mobile), couleurs de la palette.
- playlists : 2 à 5 playlists pour ranger les Shorts (par créateur, par jeu, par type de moment). Chacune : title (60 caractères maximum), description (1 à 2 phrases) et keywords : mots qui, trouvés dans le titre d'un clip ou le nom de la chaîne d'origine, l'y rangent. Une playlist « best of » avec keywords vide reçoit tous les Shorts."""

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
DEFAULT_PALETTE = ["#1F1E1D", "#D97757", "#FAF9F5"]


class PlaylistIdea(BaseModel):
    title: str
    description: str
    keywords: list[str]


class IdentityProposal(BaseModel):
    names: list[str] = Field(min_length=1)
    tagline: str
    description: str
    keywords: list[str]
    palette: list[str]
    avatar_prompt: str
    banner_prompt: str
    playlists: list[PlaylistIdea]

    @classmethod
    def fake(cls, user: str) -> "IdentityProposal":
        ctx = json.loads(user)
        base = (ctx.get("brief") or ctx.get("bot") or "Clips").split(",")[0].strip()[:18] or "Clips"
        creators = [w["channel"] for w in ctx.get("watched_channels", [])]
        return cls(
            names=[f"{base} Shorts", f"Le meilleur de {base}"[:30], f"{base} TV"],
            tagline=f"[simulé] les meilleurs moments de {base}",
            description=f"[simulé] Les moments forts de {base}, chaque jour en Shorts.",
            keywords=list(dict.fromkeys([base, "shorts", "clips", "best of", *creators])),
            palette=["#1E3A8A", "#F59E0B", "#FFFFFF"],
            avatar_prompt=f"[simulé] logo {base}",
            banner_prompt=f"[simulé] bannière {base}",
            playlists=[PlaylistIdea(title="Best of", description="Tous les Shorts.", keywords=[]),
                       *[PlaylistIdea(title=f"Moments de {c}"[:60], description=f"Les extraits de {c}.",
                                      keywords=[c]) for c in creators[:3]]],
        )


def clean_proposal(p: IdentityProposal) -> IdentityProposal:
    palette = [c for c in p.palette if HEX.match(c)][:3]
    return p.model_copy(update={
        "names": [n.strip()[:30] for n in p.names if n.strip()][:5] or ["Ma chaîne"],
        "description": p.description.strip()[:1000],
        "keywords": [k.strip() for k in p.keywords if k.strip()][:20],
        "palette": palette + DEFAULT_PALETTE[len(palette):],
        "playlists": [pl.model_copy(update={"title": pl.title.strip()[:150],
                                            "keywords": [k.strip() for k in pl.keywords if k.strip()]})
                      for pl in p.playlists if pl.title.strip()][:8],
    })


def default_choice(p: IdentityProposal) -> dict[str, Any]:
    return {
        "name": p.names[0],
        "description": p.description,
        "keywords": p.keywords,
        "playlists": [{**pl.model_dump(), "enabled": True} for pl in p.playlists],
        "banner": True,
        "account_id": None,
    }


def image_prompt(kind: str, choice: dict, proposal: dict) -> str:
    palette = ", ".join(proposal["palette"])
    if kind == "avatar":
        return (f"{proposal['avatar_prompt']}\n\nYouTube channel profile picture for « {choice['name']} ». "
                f"Square, flat bold logo centered on a solid background, readable at 48 px, survives a "
                f"circular crop, no small text. Colors: {palette}.")
    return (f"{proposal['banner_prompt']}\n\nYouTube channel banner, 16:9. Write the channel name "
            f"« {choice['name']} » and the tagline « {proposal['tagline']} » in large clean letters, "
            f"centered inside the middle band (central 60% width, 30% height): everything outside it is "
            f"background that gets cropped on phones. Colors: {palette}. No other text, no watermark.")


# -- opérations (API, CLI) ----------------------------------------------------------

def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in ("proposal", "choice", "applied"):
        out[key] = json.loads(out[key]) if out[key] else None
    return out


def latest(conn: sqlite3.Connection, bot_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM identity WHERE bot_id = ? ORDER BY id DESC LIMIT 1", (bot_id,)).fetchone()
    return row_to_dict(row) if row else None


def get(conn: sqlite3.Connection, identity_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM identity WHERE id = ?", (identity_id,)).fetchone()
    if row is None:
        raise services.ServiceError(f"identité {identity_id} introuvable")
    return row


def start(conn: sqlite3.Connection, queue: JobQueue, bot_id: int, brief: str = "",
          *, now: float | None = None) -> int:
    services.get_bot(conn, bot_id)
    now = time.time() if now is None else now
    with tx(conn):
        busy = conn.execute("SELECT 1 FROM identity WHERE bot_id = ? AND (status IN ('generating', 'applying') "
                            "OR images_status = 'generating')", (bot_id,)).fetchone()
        if busy:
            raise services.ServiceError("une proposition est déjà en cours pour ce bot")
        cur = conn.execute("INSERT INTO identity (bot_id, brief, created_at, updated_at) VALUES (?, ?, ?, ?)",
                           (bot_id, brief.strip()[:2000], now, now))
        queue.enqueue("identity_generate", {"identity_id": cur.lastrowid}, key=f"identity:{cur.lastrowid}")
    return cur.lastrowid


CHOICE_KEYS = {"name", "description", "keywords", "playlists", "banner", "account_id"}


def update_choice(conn: sqlite3.Connection, identity_id: int, changes: dict[str, Any]) -> dict:
    row = get(conn, identity_id)
    unknown = set(changes) - CHOICE_KEYS
    if unknown:
        raise services.ServiceError(f"champs inconnus : {', '.join(sorted(unknown))}")
    if row["status"] in ("generating", "applying"):
        raise services.ServiceError("proposition en cours de traitement")
    if "description" in changes and len(changes["description"] or "") > 1000:
        raise services.ServiceError("description : 1000 caractères maximum (limite YouTube)")
    if "name" in changes and not 1 <= len((changes["name"] or "").strip()) <= 50:
        raise services.ServiceError("nom : 1 à 50 caractères")
    for pl in changes.get("playlists") or []:
        if not isinstance(pl, dict) or not str(pl.get("title", "")).strip():
            raise services.ServiceError("chaque playlist doit avoir un titre")
    if changes.get("account_id") is not None:
        acc = conn.execute("SELECT platform FROM account WHERE id = ?", (changes["account_id"],)).fetchone()
        if acc is None or acc["platform"] != "youtube":
            raise services.ServiceError("compte YouTube introuvable")
    choice = {**json.loads(row["choice"]), **changes}
    with tx(conn):
        conn.execute("UPDATE identity SET choice = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(choice), time.time(), identity_id))
    return row_to_dict(get(conn, identity_id))


def regenerate_images(conn: sqlite3.Connection, queue: JobQueue, identity_id: int) -> None:
    row = get(conn, identity_id)
    if row["status"] != "ready" and row["status"] != "applied":
        raise services.ServiceError("la proposition n'est pas prête")
    if row["images_status"] == "generating":
        raise services.ServiceError("visuels déjà en cours de génération")
    now = time.time()
    with tx(conn):
        conn.execute("UPDATE identity SET images_status = 'pending', error = NULL, updated_at = ? WHERE id = ?",
                     (now, identity_id))
        queue.enqueue("identity_images", {"identity_id": identity_id}, key=f"identity_images:{identity_id}:{now}")


def cancel_images(conn: sqlite3.Connection, identity_id: int) -> None:
    """Abandonne la génération des visuels. Un appel déjà parti chez le fournisseur
    se termine, mais son résultat est ignoré."""
    row = get(conn, identity_id)
    if row["images_status"] not in ("pending", "generating"):
        raise services.ServiceError("aucune génération de visuels en cours")
    with tx(conn):
        conn.execute("UPDATE identity SET images_status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                     (services.CANCELLED, time.time(), identity_id))


def _images_cancelled(ctx, identity_id: int) -> bool:
    row = ctx.conn.execute("SELECT images_status, error FROM identity WHERE id = ?", (identity_id,)).fetchone()
    return row is not None and row["images_status"] == "failed" and services.is_cancelled(row)


def apply(conn: sqlite3.Connection, queue: JobQueue, identity_id: int) -> None:
    row = get(conn, identity_id)
    if row["status"] not in ("ready", "applied"):
        raise services.ServiceError("la proposition n'est pas prête")
    if row["images_status"] == "generating":
        raise services.ServiceError("attends la fin de la génération des visuels")
    account_id = target_account_id(conn, row)
    if account_id is None:
        raise services.ServiceError("aucune chaîne YouTube liée à ce bot : lie un compte dans Configuration")
    now = time.time()
    with tx(conn):
        conn.execute("UPDATE identity SET status = 'applying', account_id = ?, error = NULL, updated_at = ? "
                     "WHERE id = ?", (account_id, now, identity_id))
        queue.enqueue("identity_apply", {"identity_id": identity_id}, key=f"identity_apply:{identity_id}:{now}")


def target_account_id(conn: sqlite3.Connection, row: sqlite3.Row) -> int | None:
    chosen = json.loads(row["choice"]).get("account_id")
    if chosen:
        return chosen
    first = conn.execute(
        "SELECT a.id FROM account a JOIN bot_account ba ON ba.account_id = a.id "
        "WHERE ba.bot_id = ? AND a.platform = 'youtube' ORDER BY a.id LIMIT 1", (row["bot_id"],)).fetchone()
    return first["id"] if first else None


# -- jobs -----------------------------------------------------------------------------

def _set(ctx, identity_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    with tx(ctx.conn):
        ctx.conn.execute(f"UPDATE identity SET {sets}, updated_at = ? WHERE id = ?",
                         (*fields.values(), ctx.clock(), identity_id))


def _text_provider(ctx) -> AIProvider:
    from .ai.providers import make_text_provider

    if ctx.ai_override is not None:
        return ctx.ai_override
    settings = services.get_settings(ctx.conn)
    return make_text_provider(settings["identity_provider"], settings["identity_model"], effort="medium")


def context_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    bot = services.get_bot(conn, row["bot_id"])
    watches = [{"platform": w["platform"], "channel": w["channel"]} for w in conn.execute(
        "SELECT platform, channel FROM watch WHERE bot_id = ? ORDER BY id", (bot["id"],))]
    titles = [r["title"] for r in conn.execute(
        "SELECT title FROM clip WHERE bot_id = ? AND title != '' ORDER BY id DESC LIMIT 20", (bot["id"],))]
    return json.dumps({"bot": bot["name"], "brief": row["brief"], "watched_channels": watches,
                       "recent_clip_titles": titles, "language": "fr"}, ensure_ascii=False)


def run_identity_generate(ctx, payload: dict) -> dict:
    from .ai.budget import record_usage, today

    row = get_or_fail(ctx, payload["identity_id"])
    if row["proposal"] is None:
        provider = _text_provider(ctx)
        proposal, usage = provider.complete_json(SYSTEM_PROMPT, context_payload(ctx.conn, row),
                                                 IdentityProposal, max_tokens=8000)
        proposal = clean_proposal(proposal)
        record_usage(ctx.conn, row["bot_id"], today(ctx.clock()), provider,
                     usage.input_tokens, usage.output_tokens)
        with tx(ctx.conn):
            ctx.conn.execute(
                "UPDATE identity SET proposal = ?, choice = ?, status = 'ready', error = NULL, updated_at = ? "
                "WHERE id = ?", (proposal.model_dump_json(), json.dumps(default_choice(proposal)),
                                 ctx.clock(), row["id"]))
            ctx.queue.enqueue("identity_images", {"identity_id": row["id"]}, key=f"identity_images:{row['id']}")
    return {"identity_id": row["id"]}


def get_or_fail(ctx, identity_id: int) -> sqlite3.Row:
    row = ctx.conn.execute("SELECT * FROM identity WHERE id = ?", (identity_id,)).fetchone()
    if row is None:
        raise PermanentError(f"identité {identity_id} supprimée")
    return row


def run_identity_images(ctx, payload: dict) -> dict:
    from .ai.images import SvgImage, finalize, make_image_generator
    from .pipeline.cache import short_hash

    row = get_or_fail(ctx, payload["identity_id"])
    if row["proposal"] is None:
        raise PermanentError("aucune proposition à illustrer")
    if _images_cancelled(ctx, row["id"]):
        return {"cancelled": True}
    _set(ctx, row["id"], images_status="generating", error=None)
    proposal, choice = json.loads(row["proposal"]), json.loads(row["choice"])
    if ctx.image_override is not None:
        generator = ctx.image_override
    else:
        settings = services.get_settings(ctx.conn)
        text = _text_provider(ctx) if settings["image_provider"] == "svg" else None
        generator = make_image_generator(settings["image_provider"], settings["image_model"], text)
    if isinstance(generator, SvgImage):
        generator.context = {"name": choice["name"], "tagline": proposal["tagline"], "palette": proposal["palette"]}
    paths = {}
    for kind, ext in (("avatar", "png"), ("banner", "jpg")):
        if _images_cancelled(ctx, row["id"]):
            return {"cancelled": True}
        prompt = image_prompt(kind, choice, proposal)
        out = ctx.cache.path("identity", f"{row['id']}-{kind}-{short_hash(prompt, generator.name, ctx.clock())}.{ext}")
        finalize(generator.generate(prompt, kind), out, kind)
        paths[kind] = str(out)
    if _images_cancelled(ctx, row["id"]):
        return {"cancelled": True}
    _set(ctx, row["id"], images_status="ready", avatar_path=paths["avatar"], banner_path=paths["banner"])
    return paths


def _manager(ctx, bot: sqlite3.Row, platform: str) -> ChannelManager:
    if bot["dry_run"] or ctx.force_dry_run:
        return DryRunChannelManager(ctx.cache)
    if ctx.channel_override is not None:
        return ctx.channel_override(platform)
    return channel_manager_for(platform)


def _account(conn: sqlite3.Connection, account_id: int) -> Account:
    a = conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
    if a is None:
        raise PermanentError(f"compte {account_id} supprimé")
    return Account(a["id"], a["platform"], a["display_name"], a["keyring_ref"])


def run_identity_apply(ctx, payload: dict) -> dict:
    row = get_or_fail(ctx, payload["identity_id"])
    if row["account_id"] is None:
        raise PermanentError("aucun compte cible")
    bot = services.get_bot(ctx.conn, row["bot_id"])
    account = _account(ctx.conn, row["account_id"])
    manager = _manager(ctx, bot, account.platform)
    choice = json.loads(row["choice"])
    applied = json.loads(row["applied"] or "{}")
    # Chaque étape réussie est notée : une reprise après erreur ne refait pas le travail.
    done = applied.setdefault("done", {})

    def save():
        _set(ctx, row["id"], applied=json.dumps(applied))

    banner_key = f"banner:{row['banner_path']}"
    if choice.get("banner") and row["banner_path"] and banner_key not in done:
        done[banner_key] = manager.upload_banner(account, Path(row["banner_path"]))
        save()
    branding_key = "branding:" + json.dumps([choice["description"], choice["keywords"],
                                             done.get(banner_key)], ensure_ascii=False)
    if branding_key not in done:
        manager.update_branding(account, description=choice["description"], keywords=choice["keywords"],
                                banner_url=done.get(banner_key) if choice.get("banner") else None)
        done[branding_key] = True
        save()
    playlists = applied.setdefault("playlists", {})
    for pl in choice["playlists"]:
        if not pl.get("enabled", True):
            continue
        if pl["title"] not in playlists:
            playlists[pl["title"]] = manager.ensure_playlist(account, pl["title"], pl.get("description", ""))
            save()
        with tx(ctx.conn):
            ctx.conn.execute(
                """INSERT INTO playlist (bot_id, account_id, title, description, keywords, external_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (bot_id, account_id, title) DO UPDATE SET
                       description = excluded.description, keywords = excluded.keywords,
                       external_id = excluded.external_id""",
                (bot["id"], account.id, pl["title"], pl.get("description", ""),
                 json.dumps(pl.get("keywords", [])), playlists[pl["title"]], ctx.clock()))
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE identity SET status = 'applied', applied = ?, error = NULL, updated_at = ? "
                         "WHERE id = ?", (json.dumps(applied), ctx.clock(), row["id"]))
        # Les Shorts déjà publiés rejoignent aussi leurs playlists.
        for pj in ctx.conn.execute(
                "SELECT pj.id FROM publish_job pj JOIN clip c ON c.id = pj.clip_id "
                "WHERE c.bot_id = ? AND pj.account_id = ? AND pj.status = 'published'", (bot["id"], account.id)):
            ctx.queue.enqueue("playlist_add", {"publish_job_id": pj["id"]}, key=f"playlist_add:{pj['id']}:{row['id']}")
    return {"playlists": len(playlists)}


def clip_text(conn: sqlite3.Connection, clip_id: int) -> str:
    """Texte sur lequel on cherche les mots-clés des playlists."""
    parts = []
    clip = conn.execute("SELECT title, description, source_id FROM clip WHERE id = ?", (clip_id,)).fetchone()
    if clip:
        parts += [clip["title"], clip["description"]]
        for d in conn.execute(
                "SELECT d.title, w.channel FROM discovered d LEFT JOIN watch w ON w.id = d.watch_id "
                "WHERE d.source_id = ?", (clip["source_id"],)):
            parts += [d["title"] or "", d["channel"] or ""]
    return " ".join(parts).casefold()


def matches(keywords: list[str], text: str) -> bool:
    return not keywords or any(k.casefold() in text for k in keywords if k.strip())


def run_playlist_add(ctx, payload: dict) -> dict:
    pj = ctx.conn.execute(
        "SELECT pj.*, c.bot_id FROM publish_job pj JOIN clip c ON c.id = pj.clip_id WHERE pj.id = ?",
        (payload["publish_job_id"],)).fetchone()
    if pj is None or pj["status"] != "published" or not pj["external_id"]:
        return {"skipped": True}
    bot = services.get_bot(ctx.conn, pj["bot_id"])
    account = _account(ctx.conn, pj["account_id"])
    text = clip_text(ctx.conn, pj["clip_id"])
    added = []
    for pl in ctx.conn.execute(
            "SELECT * FROM playlist WHERE bot_id = ? AND account_id = ? AND external_id IS NOT NULL "
            "AND id NOT IN (SELECT playlist_id FROM playlist_item WHERE publish_job_id = ?)",
            (bot["id"], account.id, pj["id"])).fetchall():
        if not matches(json.loads(pl["keywords"]), text):
            continue
        _manager(ctx, bot, account.platform).add_to_playlist(account, pl["external_id"], pj["external_id"])
        with tx(ctx.conn):
            ctx.conn.execute("INSERT OR IGNORE INTO playlist_item (playlist_id, publish_job_id, added_at) "
                             "VALUES (?, ?, ?)", (pl["id"], pj["id"], ctx.clock()))
        added.append(pl["title"])
    return {"added": added}


def dead_generate(ctx, payload: dict, error: str) -> None:
    _set(ctx, payload["identity_id"], status="failed", error=error)


def dead_images(ctx, payload: dict, error: str) -> None:
    if not _images_cancelled(ctx, payload["identity_id"]):
        _set(ctx, payload["identity_id"], images_status="failed", error=error)


def dead_apply(ctx, payload: dict, error: str) -> None:
    # La proposition reste modifiable : l'utilisateur peut corriger puis réappliquer.
    _set(ctx, payload["identity_id"], status="ready", error=error)


IDENTITY_TASKS = {
    "identity_generate": Task(run_identity_generate, on_dead=dead_generate),
    "identity_images": Task(run_identity_images, on_dead=dead_images),
    "identity_apply": Task(run_identity_apply, on_dead=dead_apply),
    "playlist_add": Task(run_playlist_add),
}
