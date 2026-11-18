"""Éclaireur IA : à partir d'un thème (« GTA 6, streamers FR »), trouve les
chaînes Twitch et YouTube à surveiller, puis les ajoute aux surveillances du bot.

1. L'IA transforme le thème en recherches (catégories Twitch, mots-clés, requêtes).
2. Les API officielles listent les candidats (lives en cours, vidéos récentes).
3. L'IA choisit les plus prometteurs pour le clipping, avec une raison.

Sans IA configurée : le thème sert tel quel de recherche et les chaînes les plus
regardées sont retenues. Les chaînes trouvées alimentent la découverte habituelle
(VODs, pics de chat, notation). Celles que l'utilisateur retire ne reviennent pas.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time

from pydantic import BaseModel, Field

from . import services
from .ai.base import AIProvider
from .ai.budget import record_usage, today, used_tokens
from .db import tx
from .discovery import ChannelCandidate, get_discoverer
from .jobs.queue import JobQueue, PermanentError
from .jobs.worker import Task

log = logging.getLogger(__name__)

PLAN_PROMPT = """Tu aides une chaîne de Shorts à trouver des créateurs à clipper (streams Twitch, vidéos YouTube).

À partir du thème donné, renvoie les recherches à lancer :
- twitch_categories : 1 à 3 noms exacts de catégories Twitch (souvent le nom officiel du jeu, en anglais, ex. « Grand Theft Auto VI », « Just Chatting ») ;
- keywords : 1 à 3 mots-clés courts pour chercher des chaînes Twitch par nom ou titre ;
- youtube_queries : 1 à 2 requêtes de recherche YouTube pour trouver des vidéos récentes et populaires du thème.
Respecte la langue demandée pour les mots-clés et requêtes."""

PICK_PROMPT = """Tu choisis les chaînes à surveiller pour en tirer des Shorts viraux (moments forts, réactions, humour, action).

On te donne le thème, la langue voulue et des chaînes candidates (titre du live ou de la vidéo, catégorie, langue, audience). Retiens au plus le nombre demandé, en privilégiant : correspondance au thème, langue, audience régulière et engagée, contenu riche en réactions et en moments forts. Écarte les chaînes hors sujet, les rediffusions, les comptes de presse ou de bandes-annonces officielles.

Pour chaque chaîne retenue : platform, channel_id (repris exactement), score de 0 à 100, reason (une phrase en français)."""


class ScoutPlan(BaseModel):
    twitch_categories: list[str]
    keywords: list[str]
    youtube_queries: list[str]

    @classmethod
    def fake(cls, user: str) -> "ScoutPlan":
        theme = json.loads(user)["theme"]
        return cls(twitch_categories=[theme], keywords=[theme], youtube_queries=[theme])


class PickedChannel(BaseModel):
    platform: str
    channel_id: str
    score: int = Field(ge=0, le=100)
    reason: str


class ScoutPick(BaseModel):
    channels: list[PickedChannel]

    @classmethod
    def fake(cls, user: str) -> "ScoutPick":
        req = json.loads(user)
        ranked = sorted(req["candidates"], key=lambda c: -c["viewers"])[:req["max"]]
        return cls(channels=[PickedChannel(platform=c["platform"], channel_id=c["channel_id"], score=70,
                                           reason=f"[simulé] {c['viewers']} spectateurs sur « {c['title'][:40]} »")
                             for c in ranked])


def start(conn: sqlite3.Connection, queue: JobQueue, bot_id: int, *, now: float | None = None) -> None:
    bot = services.get_bot(conn, bot_id)
    ai = services.ai_settings(bot)
    if not str(ai["scout_theme"]).strip():
        raise services.ServiceError("décris d'abord un thème (ex. « GTA 6, streamers FR »)")
    now = time.time() if now is None else now
    queue.enqueue("scout", {"bot_id": bot_id}, key=f"scout:{bot_id}:{int(now)}")


def enqueue_due_scouts(conn: sqlite3.Connection, queue: JobQueue, now: float) -> int:
    n = 0
    for bot in conn.execute("SELECT * FROM bot"):
        ai = services.ai_settings(bot)
        if not ai["scout_enabled"] or not str(ai["scout_theme"]).strip():
            continue
        period = ai["scout_every_hours"] * 3600
        last = conn.execute("SELECT MAX(created_at) FROM scout_run WHERE bot_id = ?", (bot["id"],)).fetchone()[0]
        if last is None or last + period <= now:
            queue.enqueue("scout", {"bot_id": bot["id"]}, key=f"scout:{bot['id']}:{int(now // period)}")
            n += 1
    return n


def _provider(ctx, bot: sqlite3.Row, ai: dict) -> AIProvider | None:
    """L'éclaireur coûte très peu (deux appels par passage) : il utilise le vrai
    fournisseur même en dry run, sauf si le budget du jour est atteint."""
    from .ai.providers import make_text_provider

    if ctx.ai_override is not None:
        return ctx.ai_override
    if ai["provider"] == "none":
        return None
    if used_tokens(ctx.conn, bot["id"], today(ctx.clock())) >= ai["daily_token_budget"]:
        return None
    return make_text_provider(ai["provider"], ai["model"], effort="low")


def _ask(ctx, provider: AIProvider, bot_id: int, system: str, payload: dict, schema):
    result, usage = provider.complete_json(system, json.dumps(payload, ensure_ascii=False), schema, max_tokens=4000)
    record_usage(ctx.conn, bot_id, today(ctx.clock()), provider, usage.input_tokens, usage.output_tokens)
    return result


def run_scout(ctx, payload: dict) -> dict:
    bot = services.get_bot(ctx.conn, payload["bot_id"])
    ai = services.ai_settings(bot)
    theme, language = str(ai["scout_theme"]).strip(), ai["scout_language"] or ""
    if not theme:
        return {"skipped": "aucun thème"}
    with tx(ctx.conn):
        run_id = ctx.conn.execute("INSERT INTO scout_run (bot_id, created_at) VALUES (?, ?)",
                                  (bot["id"], ctx.clock())).lastrowid
    try:
        result = _scout(ctx, bot, ai, theme, language, run_id)
    except Exception as exc:
        with tx(ctx.conn):
            ctx.conn.execute("UPDATE scout_run SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                             (f"{type(exc).__name__}: {exc}", ctx.clock(), run_id))
        raise
    return result


def _scout(ctx, bot: sqlite3.Row, ai: dict, theme: str, language: str, run_id: int) -> dict:
    # Places libres : les chaînes de l'IA encore actives comptent dans la limite.
    active = ctx.conn.execute("SELECT COUNT(*) FROM watch WHERE bot_id = ? AND origin = 'ai' AND dismissed = 0",
                              (bot["id"],)).fetchone()[0]
    room = ai["scout_max_channels"] - active
    known = {(r["platform"], r["channel_id"]) for r in ctx.conn.execute(
        "SELECT platform, channel_id FROM watch WHERE bot_id = ? AND channel_id IS NOT NULL", (bot["id"],))}
    known |= {(r["platform"], r["channel"]) for r in ctx.conn.execute(
        "SELECT platform, channel FROM watch WHERE bot_id = ?", (bot["id"],))}

    provider = _provider(ctx, bot, ai)
    if provider is not None:
        plan = _ask(ctx, provider, bot["id"], PLAN_PROMPT, {"theme": theme, "language": language}, ScoutPlan)
    else:
        plan = ScoutPlan(twitch_categories=[theme], keywords=[theme], youtube_queries=[theme])

    candidates: list[ChannelCandidate] = []
    errors = []
    for platform in ai["scout_platforms"]:
        try:
            disc = get_discoverer(platform)
            if platform == "twitch":
                found = disc.search_channels(categories=plan.twitch_categories, keywords=plan.keywords,
                                             language=language)
            else:
                found = disc.search_channels(categories=[], keywords=plan.youtube_queries, language=language)
        except PermanentError as exc:  # clés absentes pour une plateforme : on continue avec l'autre
            errors.append(f"{platform} : {exc}")
            continue
        candidates += [c for c in found if (c.platform, c.channel_id) not in known
                       and (c.platform, c.channel) not in known]

    added = []
    if room > 0 and candidates:
        by_key = {(c.platform, c.channel_id): c for c in candidates}
        if provider is not None:
            pick = _ask(ctx, provider, bot["id"], PICK_PROMPT, {
                "theme": theme, "language": language, "max": room,
                "candidates": [c.brief() for c in candidates[:80]]}, ScoutPick)
            chosen = [(by_key[(p.platform, p.channel_id)], p.score, p.reason)
                      for p in sorted(pick.channels, key=lambda p: -p.score)
                      if (p.platform, p.channel_id) in by_key]
        else:
            chosen = [(c, None, f"{c.viewers} spectateurs · {c.title[:80]}")
                      for c in sorted(candidates, key=lambda c: -c.viewers)]
        now = ctx.clock()
        with tx(ctx.conn):
            for cand, score, reason in chosen[:room]:
                cur = ctx.conn.execute(
                    """INSERT OR IGNORE INTO watch (bot_id, platform, channel, channel_id, interval_minutes,
                           origin, reason, score, created_at)
                       VALUES (?, ?, ?, ?, 60, 'ai', ?, ?, ?)""",
                    (bot["id"], cand.platform, cand.channel, cand.channel_id, reason[:300], score, now))
                if cur.rowcount:
                    added.append({"platform": cand.platform, "channel": cand.channel, "name": cand.name,
                                  "reason": reason[:300], "score": score})
    # Aucune plateforme utilisable (clés absentes) : échec affiché, sans nouvel essai.
    status = "failed" if errors and not candidates else "done"
    with tx(ctx.conn):
        ctx.conn.execute(
            "UPDATE scout_run SET status = ?, plan = ?, candidates = ?, added = ?, error = ?, finished_at = ? "
            "WHERE id = ?",
            (status, plan.model_dump_json(), len(candidates), json.dumps(added, ensure_ascii=False),
             "; ".join(errors) or None, ctx.clock(), run_id))
    return {"candidates": len(candidates), "added": len(added)}


SCOUT_TASKS = {"scout": Task(run_scout)}
