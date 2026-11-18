"""Budget de tokens par bot et par jour, cache des réponses et alertes.

Toute notation passe par `score_with_budget` : cache d'abord (0 token), puis
estimation, puis appel seulement si le budget du jour le permet.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime

from ..db import tx
from .base import PROMPT_VERSION, AIProvider, ScoringRequest, ScoringResult, SegmentScore, validate_result


def today(clock: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if clock is None else clock).date().isoformat()


def used_tokens(conn: sqlite3.Connection, bot_id: int, day: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM ai_usage WHERE bot_id = ? AND day = ?",
        (bot_id, day)).fetchone()
    return row[0]


def record_usage(conn: sqlite3.Connection, bot_id: int, day: str, provider: AIProvider,
                 input_tokens: int, output_tokens: int) -> None:
    with tx(conn):
        conn.execute(
            """INSERT INTO ai_usage (bot_id, day, provider, model, input_tokens, output_tokens, calls)
               VALUES (?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT (bot_id, day, provider, model) DO UPDATE SET
                   input_tokens = input_tokens + excluded.input_tokens,
                   output_tokens = output_tokens + excluded.output_tokens,
                   calls = calls + 1""",
            (bot_id, day, provider.name, provider.model, input_tokens, output_tokens))


def raise_alert(conn: sqlite3.Connection, bot_id: int | None, kind: str, message: str,
                now: float | None = None) -> bool:
    """Une alerte par (bot, type, jour). Renvoie True si elle est nouvelle."""
    now = time.time() if now is None else now
    with tx(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO alert (bot_id, kind, day, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (bot_id, kind, today(now), message, now))
    return cur.rowcount == 1


def cache_key(provider: AIProvider, request: ScoringRequest) -> str:
    raw = json.dumps([PROMPT_VERSION, provider.name, provider.model, request.user_content()])
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class Outcome:
    scores: dict[str, SegmentScore] | None  # None = budget atteint, rien envoyé
    tokens: int  # tokens réellement consommés par cet appel (0 si cache)
    cached: bool = False


def score_with_budget(conn: sqlite3.Connection, provider: AIProvider, request: ScoringRequest, *,
                      bot_id: int, daily_budget: int, now: float | None = None) -> Outcome:
    now = time.time() if now is None else now
    key = cache_key(provider, request)
    hit = conn.execute("SELECT response FROM ai_cache WHERE key = ?", (key,)).fetchone()
    if hit:
        result = ScoringResult.model_validate_json(hit["response"])
        return Outcome(validate_result(request, result), 0, cached=True)

    day = today(now)
    used = used_tokens(conn, bot_id, day)
    estimate = provider.estimate_tokens(request)
    if used + estimate > daily_budget:
        raise_alert(conn, bot_id, "token_budget",
                    f"Budget IA du jour atteint ({used} / {daily_budget} tokens) : "
                    f"analyse reportée à demain.", now)
        return Outcome(None, 0)

    result, usage = provider.score(request)
    record_usage(conn, bot_id, day, provider, usage.input_tokens, usage.output_tokens)
    with tx(conn):
        conn.execute("INSERT OR REPLACE INTO ai_cache (key, response, created_at) VALUES (?, ?, ?)",
                     (key, result.model_dump_json(), now))
    if used_tokens(conn, bot_id, day) >= daily_budget:
        raise_alert(conn, bot_id, "token_budget",
                    f"Budget IA du jour épuisé ({daily_budget} tokens) : les analyses suivantes "
                    f"attendront demain.", now)
    return Outcome(validate_result(request, result), usage.total)
