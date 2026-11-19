"""API HTTP locale (127.0.0.1) : routes sous /api, interface web servie à la racine."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, db, eta, secrets, services
from .config import Paths
from .jobs.queue import JobQueue

WEB_DIR = Path(__file__).parent / "web"


class BotIn(BaseModel):
    name: str
    dry_run: bool | None = None
    clip_settings: dict[str, Any] | None = None


class BotPatch(BaseModel):
    name: str | None = None
    dry_run: bool | None = None
    clip_settings: dict[str, Any] | None = None
    ai_settings: dict[str, Any] | None = None


class WatchPatch(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None


class WatchIn(BaseModel):
    platform: str
    channel: str
    interval_minutes: int = 60


class SourceIn(BaseModel):
    uri: str


class ScheduleIn(BaseModel):
    every_hours: float
    days: str = "all"
    window: str = "00:00-00:00"
    timezone: str = "UTC"
    enabled: bool = True


class IdentityIn(BaseModel):
    brief: str = ""


class HookIn(BaseModel):
    hook: str


class ApproveIn(BaseModel):
    title: str | None = None
    description: str | None = None


def _replan(conn: sqlite3.Connection) -> None:
    """Créneaux recalculés tout de suite, sans attendre le prochain tour du démon."""
    from .scheduler import Scheduler

    Scheduler(conn, JobQueue(conn)).tick()


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


def create_app(home: Path) -> FastAPI:
    paths = Paths(home).ensure()
    app = FastAPI(title="AutoClip", version=__version__)
    api = APIRouter(prefix="/api")

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(paths.db)
        try:
            yield conn
        finally:
            conn.close()

    def guard(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except services.ServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    # -- état général ---------------------------------------------------------
    @api.get("/health")
    def health():
        return {"ok": True, "version": __version__}

    @api.get("/status")
    def status(conn=Depends(get_conn)):
        return {
            "version": __version__,
            "home": str(paths.home),
            "jobs": JobQueue(conn).stats(),
            "review": conn.execute("SELECT COUNT(*) FROM clip WHERE status = 'review'").fetchone()[0],
            "alerts": conn.execute("SELECT COUNT(*) FROM alert WHERE seen = 0").fetchone()[0],
        }

    # -- bots -------------------------------------------------------------------
    @api.get("/bots")
    def list_bots(conn=Depends(get_conn)):
        bots = _rows(conn.execute("SELECT id, name, dry_run, created_at FROM bot ORDER BY id"))
        for bot in bots:
            bot["stats"] = services.bot_stats(conn, bot["id"])
        return bots

    @api.post("/bots", status_code=201)
    def create_bot(body: BotIn, conn=Depends(get_conn)):
        return {"id": guard(services.create_bot, conn, body.name, dry_run=body.dry_run,
                            clip_settings=body.clip_settings)}

    @api.get("/bots/{bot_id}")
    def get_bot(bot_id: int, conn=Depends(get_conn)):
        row = guard(services.get_bot, conn, bot_id)
        bot = dict(row)
        bot["clip_settings"] = services.bot_settings(row)
        bot["sources"] = _rows(conn.execute(
            "SELECT id, kind, uri, status, error, created_at FROM source WHERE bot_id = ? ORDER BY id DESC",
            (bot_id,)))
        if any(s["status"] in services.ACTIVE_SOURCE for s in bot["sources"]):
            remaining = eta.estimate(conn, time.time())
            linked = {r["source_id"]: r["id"] for r in conn.execute(
                "SELECT id, source_id FROM discovered WHERE bot_id = ? AND source_id IS NOT NULL", (bot_id,))}
            for s in bot["sources"]:
                if s["status"] in services.ACTIVE_SOURCE:
                    key = ("d", linked[s["id"]]) if s["id"] in linked else ("s", s["id"])
                    s["eta_seconds"] = remaining.get(key)
        bot["accounts"] = [
            {"id": r["id"], "platform": r["platform"], "display_name": r["display_name"],
             "url": services.account_url(r["platform"], r["keyring_ref"])}
            for r in conn.execute("SELECT a.id, a.platform, a.display_name, a.keyring_ref FROM account a "
                                  "JOIN bot_account ba ON ba.account_id = a.id WHERE ba.bot_id = ? ORDER BY a.id",
                                  (bot_id,))]
        sched = conn.execute("SELECT * FROM schedule WHERE bot_id = ?", (bot_id,)).fetchone()
        bot["schedule"] = dict(sched) if sched else None
        bot["stats"] = services.bot_stats(conn, bot_id)
        bot["ai_settings"] = services.ai_settings(row)
        bot["watches"] = _rows(conn.execute("SELECT * FROM watch WHERE bot_id = ? AND dismissed = 0 ORDER BY id",
                                            (bot_id,)))
        return bot

    @api.patch("/bots/{bot_id}")
    def patch_bot(bot_id: int, body: BotPatch, conn=Depends(get_conn)):
        guard(services.update_bot, conn, bot_id, **body.model_dump())
        return {"ok": True}

    @api.delete("/bots/{bot_id}")
    def delete_bot(bot_id: int, conn=Depends(get_conn)):
        guard(services.delete_bot, conn, bot_id)
        return {"ok": True}

    @api.post("/bots/{bot_id}/sources", status_code=201)
    def add_source(bot_id: int, body: SourceIn, conn=Depends(get_conn)):
        return {"id": guard(services.add_source, conn, JobQueue(conn), bot_id, body.uri)}

    @api.put("/bots/{bot_id}/schedule")
    def put_schedule(bot_id: int, body: ScheduleIn, conn=Depends(get_conn)):
        guard(services.set_schedule, conn, bot_id, **body.model_dump())
        _replan(conn)
        return {"ok": True}

    @api.post("/bots/{bot_id}/accounts/{account_id}")
    def link(bot_id: int, account_id: int, conn=Depends(get_conn)):
        guard(services.link_account, conn, bot_id, account_id)
        return {"ok": True}

    @api.delete("/bots/{bot_id}/accounts/{account_id}")
    def unlink(bot_id: int, account_id: int, conn=Depends(get_conn)):
        services.unlink_account(conn, bot_id, account_id)
        return {"ok": True}

    @api.get("/bots/{bot_id}/stats")
    def stats(bot_id: int, days: int = 14, conn=Depends(get_conn)):
        guard(services.get_bot, conn, bot_id)
        today = datetime.now().date()
        first = today - timedelta(days=days - 1)
        per_day = {(first + timedelta(days=i)).isoformat(): 0 for i in range(days)}
        rows = conn.execute(
            """SELECT pj.updated_at FROM publish_job pj JOIN clip c ON c.id = pj.clip_id
               WHERE c.bot_id = ? AND pj.status = 'published' AND pj.updated_at >= ?""",
            (bot_id, datetime.combine(first, datetime.min.time()).timestamp()))
        for r in rows:
            day = datetime.fromtimestamp(r["updated_at"]).date().isoformat()
            if day in per_day:
                per_day[day] += 1
        nxt = conn.execute(
            """SELECT MIN(pj.scheduled_at) FROM publish_job pj JOIN clip c ON c.id = pj.clip_id
               WHERE c.bot_id = ? AND pj.status = 'pending' AND pj.scheduled_at IS NOT NULL""",
            (bot_id,)).fetchone()[0]
        return {
            "clips": services.bot_stats(conn, bot_id),
            "sources": conn.execute("SELECT COUNT(*) FROM source WHERE bot_id = ?", (bot_id,)).fetchone()[0],
            "published_per_day": [{"day": d, "count": n} for d, n in per_day.items()],
            "next_publication": nxt,
        }

    # -- découverte automatique et IA ---------------------------------------------
    @api.get("/bots/{bot_id}/watches")
    def watches(bot_id: int, conn=Depends(get_conn)):
        guard(services.get_bot, conn, bot_id)
        return _rows(conn.execute("SELECT * FROM watch WHERE bot_id = ? AND dismissed = 0 ORDER BY id", (bot_id,)))

    @api.patch("/watches/{watch_id}")
    def patch_watch(watch_id: int, body: WatchPatch, conn=Depends(get_conn)):
        guard(services.update_watch, conn, watch_id, **body.model_dump())
        return {"ok": True}

    @api.post("/bots/{bot_id}/discover")
    def discover_now(bot_id: int, conn=Depends(get_conn)):
        """Vérifie tout de suite les chaînes surveillées du bot."""
        guard(services.get_bot, conn, bot_id)
        queue, now = JobQueue(conn), time.time()
        rows = conn.execute("SELECT id FROM watch WHERE bot_id = ? AND enabled = 1 AND dismissed = 0", (bot_id,)).fetchall()
        for w in rows:
            queue.enqueue("discover", {"watch_id": w["id"]}, key=f"discover:{w['id']}:now:{int(now)}")
        return {"watches": len(rows)}

    @api.post("/bots/{bot_id}/scout")
    def scout_now(bot_id: int, conn=Depends(get_conn)):
        from . import scout

        guard(scout.start, conn, JobQueue(conn), bot_id)
        return {"ok": True}

    @api.get("/bots/{bot_id}/scout")
    def scout_runs(bot_id: int, conn=Depends(get_conn)):
        guard(services.get_bot, conn, bot_id)
        runs = _rows(conn.execute("SELECT * FROM scout_run WHERE bot_id = ? ORDER BY id DESC LIMIT 5", (bot_id,)))
        for r in runs:
            r["plan"] = json.loads(r["plan"]) if r["plan"] else None
            r["added"] = json.loads(r["added"])
        pending = conn.execute("SELECT COUNT(*) FROM job WHERE kind = 'scout' AND status IN ('queued', 'running') "
                               "AND json_extract(payload, '$.bot_id') = ?", (bot_id,)).fetchone()[0]
        return {"runs": runs, "pending": bool(pending)}

    @api.post("/bots/{bot_id}/watches", status_code=201)
    def add_watch(bot_id: int, body: WatchIn, conn=Depends(get_conn)):
        return {"id": guard(services.add_watch, conn, bot_id, body.platform, body.channel, body.interval_minutes)}

    @api.delete("/watches/{watch_id}")
    def delete_watch(watch_id: int, conn=Depends(get_conn)):
        guard(services.remove_watch, conn, watch_id)
        return {"ok": True}

    @api.get("/bots/{bot_id}/discovered")
    def discovered(bot_id: int, conn=Depends(get_conn)):
        rows = _rows(conn.execute("SELECT * FROM discovered WHERE bot_id = ? ORDER BY id DESC LIMIT 200", (bot_id,)))
        if any(r["status"] in services.ACTIVE_DISCOVERED for r in rows):
            remaining = eta.estimate(conn, time.time())
            for r in rows:
                if r["status"] in services.ACTIVE_DISCOVERED and r["status"] != "deferred":
                    r["eta_seconds"] = remaining.get(("d", r["id"]))
        return rows

    @api.post("/discovered/{discovered_id}/cancel")
    def cancel_discovered(discovered_id: int, conn=Depends(get_conn)):
        guard(services.cancel_discovered, conn, discovered_id)
        return {"ok": True}

    @api.post("/sources/{source_id}/cancel")
    def cancel_source(source_id: int, conn=Depends(get_conn)):
        guard(services.cancel_source, conn, source_id)
        return {"ok": True}

    @api.get("/bots/{bot_id}/ai-usage")
    def ai_usage(bot_id: int, conn=Depends(get_conn)):
        from .ai.budget import today, used_tokens

        bot = guard(services.get_bot, conn, bot_id)
        day = today()
        return {"day": day, "used": used_tokens(conn, bot_id, day),
                "budget": services.ai_settings(bot)["daily_token_budget"],
                "history": _rows(conn.execute("SELECT * FROM ai_usage WHERE bot_id = ? ORDER BY day DESC LIMIT 60",
                                              (bot_id,)))}

    @api.get("/alerts")
    def alerts(unseen: bool = False, conn=Depends(get_conn)):
        return _rows(services.list_alerts(conn, unseen_only=unseen))

    @api.post("/alerts/{alert_id}/seen")
    def alert_seen(alert_id: int, conn=Depends(get_conn)):
        with db.tx(conn):
            conn.execute("UPDATE alert SET seen = 1 WHERE id = ?", (alert_id,))
        return {"ok": True}

    twitch_login: dict[str, Any] = {}

    @api.post("/twitch/login")
    def twitch_login_start():
        """Renvoie le code à saisir sur twitch.tv/activate ; la fin de la connexion
        se fait en arrière-plan (voir GET /api/twitch/status)."""
        import threading

        from .discovery import twitch

        try:
            device = twitch.start_device_login()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from exc
        twitch_login.clear()
        twitch_login.update(state="pending")

        def finish():
            try:
                twitch_login.update(state="connected", login=twitch.finish_device_login(device))
            except Exception as exc:  # noqa: BLE001
                twitch_login.update(state="error", error=str(exc))

        threading.Thread(target=finish, daemon=True).start()
        return {"verification_uri": device["verification_uri"], "user_code": device["user_code"],
                "expires_in": device["expires_in"]}

    @api.get("/twitch/status")
    def twitch_status():
        token = secrets.get_json("token:twitch:user")
        return {"connected": bool(token), "login": token.get("login") if token else None,
                "login_flow": twitch_login or None}

    # -- agent identité de chaîne --------------------------------------------------
    from . import identity as ident

    @api.get("/bots/{bot_id}/identity")
    def get_identity(bot_id: int, conn=Depends(get_conn)):
        guard(services.get_bot, conn, bot_id)
        identity = ident.latest(conn, bot_id)
        if identity and identity["images_status"] in ("pending", "generating"):
            identity["images_eta"] = eta.estimate(conn, time.time()).get(("i", identity["id"]))
        return {"identity": identity,
                "playlists": _rows(conn.execute(
                    "SELECT p.*, (SELECT COUNT(*) FROM playlist_item i WHERE i.playlist_id = p.id) AS items "
                    "FROM playlist p WHERE p.bot_id = ? ORDER BY p.id", (bot_id,)))}

    @api.post("/bots/{bot_id}/identity", status_code=201)
    def start_identity(bot_id: int, body: IdentityIn, conn=Depends(get_conn)):
        return {"id": guard(ident.start, conn, JobQueue(conn), bot_id, body.brief)}

    @api.patch("/identity/{identity_id}")
    def patch_identity(identity_id: int, changes: dict[str, Any], conn=Depends(get_conn)):
        return guard(ident.update_choice, conn, identity_id, changes)

    @api.post("/identity/{identity_id}/images")
    def identity_images(identity_id: int, conn=Depends(get_conn)):
        guard(ident.regenerate_images, conn, JobQueue(conn), identity_id)
        return {"ok": True}

    @api.post("/identity/{identity_id}/images/cancel")
    def identity_images_cancel(identity_id: int, conn=Depends(get_conn)):
        guard(ident.cancel_images, conn, identity_id)
        return {"ok": True}

    @api.post("/identity/{identity_id}/apply")
    def identity_apply(identity_id: int, conn=Depends(get_conn)):
        guard(ident.apply, conn, JobQueue(conn), identity_id)
        return {"ok": True}

    @api.get("/identity/{identity_id}/image/{kind}")
    def identity_image(identity_id: int, kind: str, conn=Depends(get_conn)):
        if kind not in ("avatar", "banner"):
            raise HTTPException(404, "image inconnue")
        row = guard(ident.get, conn, identity_id)
        path = row[f"{kind}_path"]
        if not path or not Path(path).is_file():
            raise HTTPException(404, "image pas encore générée")
        name = f"{kind}{Path(path).suffix}"
        return FileResponse(path, filename=name, headers={"Cache-Control": "no-store"})

    @api.get("/ai/providers")
    def ai_providers():
        from .ai.images import DEFAULT_IMAGE_MODELS, IMAGE_PROVIDERS
        from .ai.providers import DEFAULT_MODELS, TEXT_PROVIDERS

        return {"text": {p: DEFAULT_MODELS.get(p) for p in TEXT_PROVIDERS},
                "image": {p: DEFAULT_IMAGE_MODELS.get(p) for p in IMAGE_PROVIDERS}}

    @api.get("/ai/models/{provider}")
    def ai_models(provider: str, kind: str = "text"):
        """Modèles réellement proposés par le fournisseur (avec la clé enregistrée)."""
        from .ai.base import AIProviderError
        from .ai.providers import make_text_provider

        try:
            models = make_text_provider(provider).list_models()
        except AIProviderError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 : réseau, quota...
            raise HTTPException(502, f"{provider} : {exc}") from exc
        is_image = lambda m: "image" in m or m.startswith("dall-e")  # noqa: E731
        if kind == "image":
            models = [m for m in models if is_image(m)]
        else:
            models = [m for m in models if not is_image(m)]
        return {"models": models}

    # -- clips et file d'approbation -------------------------------------------
    @api.get("/clips")
    def clips(bot_id: int | None = None, status: str | None = None, conn=Depends(get_conn)):
        rows = _rows(services.list_clips(conn, bot_id=bot_id, status=status))
        for r in rows:
            r.pop("words", None)
        return rows

    @api.get("/clips/{clip_id}/video")
    def clip_video(clip_id: int, conn=Depends(get_conn)):
        row = conn.execute("SELECT file_path FROM clip WHERE id = ?", (clip_id,)).fetchone()
        if row is None or not row["file_path"] or not Path(row["file_path"]).is_file():
            raise HTTPException(404, "vidéo indisponible")
        return FileResponse(row["file_path"], media_type="video/mp4")

    @api.post("/clips/{clip_id}/approve")
    def approve(clip_id: int, body: ApproveIn | None = None, conn=Depends(get_conn)):
        body = body or ApproveIn()
        return {"publish_jobs": guard(services.approve_clip, conn, clip_id, title=body.title,
                                      description=body.description)}

    @api.post("/bots/{bot_id}/clips/approve-all")
    def approve_all(bot_id: int, body: dict[int, ApproveIn] | None = None, conn=Depends(get_conn)):
        edits = {cid: e.model_dump(exclude_none=True) for cid, e in (body or {}).items()}
        n = guard(services.approve_all, conn, bot_id, edits=edits)
        _replan(conn)
        return {"approved": n}

    @api.post("/publish-jobs/{publish_job_id}/publish-now")
    def publish_now(publish_job_id: int, conn=Depends(get_conn)):
        guard(services.publish_now, conn, JobQueue(conn), publish_job_id)
        _replan(conn)
        return {"ok": True}

    @api.post("/clips/{clip_id}/reject")
    def reject(clip_id: int, conn=Depends(get_conn)):
        guard(services.reject_clip, conn, clip_id)
        return {"ok": True}

    @api.get("/publish-jobs")
    def publish_jobs(bot_id: int | None = None, conn=Depends(get_conn)):
        query = """SELECT pj.*, c.title, c.bot_id, a.display_name AS account_name, a.platform,
                          a.keyring_ref AS account_ref
                   FROM publish_job pj JOIN clip c ON c.id = pj.clip_id
                   JOIN account a ON a.id = pj.account_id"""
        params: list[Any] = []
        if bot_id is not None:
            query += " WHERE c.bot_id = ?"
            params.append(bot_id)
        rows = _rows(conn.execute(query + " ORDER BY COALESCE(pj.scheduled_at, 1e18), pj.id", params))
        for r in rows:
            r["account_url"] = services.account_url(r["platform"], r.pop("account_ref"))
        return rows

    # -- file de jobs -------------------------------------------------------------
    @api.get("/jobs")
    def jobs(status: str | None = None, limit: int = 100, conn=Depends(get_conn)):
        query, params = "SELECT * FROM job", []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        rows = _rows(conn.execute(query + " ORDER BY id DESC LIMIT ?", (*params, limit)))
        return {"stats": JobQueue(conn).stats(), "jobs": rows}

    @api.put("/clips/{clip_id}/hook")
    def put_hook(clip_id: int, body: HookIn, conn=Depends(get_conn)):
        guard(services.set_hook, conn, JobQueue(conn), clip_id, body.hook)
        return {"ok": True}

    @api.post("/jobs/{job_id}/retry")
    def retry(job_id: int, conn=Depends(get_conn)):
        if not JobQueue(conn).retry(job_id):
            raise HTTPException(400, "job introuvable ou pas en échec")
        return {"ok": True}

    # -- réglages, clés d'API, comptes ------------------------------------------
    @api.get("/settings")
    def get_settings(conn=Depends(get_conn)):
        return services.get_settings(conn)

    @api.put("/settings")
    def put_settings(changes: dict[str, Any], conn=Depends(get_conn)):
        return guard(services.update_settings, conn, changes)

    @api.get("/credentials")
    def credentials():
        """Quelles clés sont enregistrées (jamais leur valeur)."""
        return {"fields": secrets.CREDENTIAL_FIELDS, "configured": secrets.credentials_status()}

    @api.put("/credentials/{platform}")
    def put_credentials(platform: str, values: dict[str, Any]):
        try:
            if platform == "youtube" and ("installed" in values or "web" in values):
                values = secrets.youtube_credentials_from_client_secret(values)
            secrets.set_credentials(platform, values)
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @api.post("/credentials/{platform}/test")
    async def test_credentials(platform: str):
        """Appel gratuit à la plateforme pour vérifier la clé enregistrée."""
        from . import keycheck

        try:
            result = await run_in_threadpool(keycheck.check, platform)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return result.to_dict()

    @api.delete("/credentials/{platform}")
    def delete_credentials(platform: str):
        secrets.delete(secrets.credentials_key(platform))
        return {"ok": True}

    @api.get("/accounts")
    def accounts(conn=Depends(get_conn)):
        from .publishers.youtube import has_manage_scope

        rows = conn.execute("SELECT id, platform, display_name, keyring_ref, created_at FROM account ORDER BY id")
        out = []
        for r in rows:
            item = dict(r)
            ref = item.pop("keyring_ref")
            item["connected"] = ref is not None
            item["url"] = services.account_url(item["platform"], ref)
            # Jeton antérieur à l'agent identité : il faut reconnecter pour gérer la chaîne.
            item["can_manage"] = item["platform"] == "youtube" and ref is not None and has_manage_scope(ref)
            out.append(item)
        return out

    youtube_connect: dict[str, Any] = {}

    @api.post("/accounts/{platform}/connect")
    def connect(platform: str):
        """Démarre la connexion OAuth : renvoie l'URL Google tout de suite, le retour
        est attendu en arrière-plan (GET pour suivre, DELETE pour annuler)."""
        if platform != "youtube":
            raise HTTPException(400, f"{platform} n'est pas encore pris en charge (youtube uniquement)")
        import threading
        import webbrowser

        from .publishers.youtube import ConnectFlow, OAuthCancelled, save_connected_account

        if (old := youtube_connect.get("flow")) is not None:
            old.cancel()
        try:
            flow = ConnectFlow()
        except Exception as exc:  # noqa: BLE001 : clés absentes...
            raise HTTPException(400, str(exc)) from exc
        youtube_connect.clear()
        youtube_connect.update(flow=flow, state="pending", auth_url=flow.auth_url)

        def wait():
            try:
                creds = flow.wait_credentials()
                conn = db.connect(paths.db)
                try:
                    account_id = save_connected_account(conn, creds)
                finally:
                    conn.close()
                if youtube_connect.get("flow") is flow:
                    youtube_connect.update(state="connected", account_id=account_id)
            except OAuthCancelled:
                if youtube_connect.get("flow") is flow:
                    youtube_connect.update(state="cancelled")
            except Exception as exc:  # noqa: BLE001
                if youtube_connect.get("flow") is flow:
                    youtube_connect.update(state="error", error=str(exc))

        threading.Thread(target=wait, daemon=True, name="youtube-oauth").start()
        webbrowser.open(flow.auth_url, new=1)
        return {"auth_url": flow.auth_url, "timeout": int(flow.deadline - time.time())}

    @api.get("/accounts/{platform}/connect")
    def connect_status(platform: str):
        return {k: v for k, v in youtube_connect.items() if k != "flow"} or {"state": "idle"}

    @api.delete("/accounts/{platform}/connect")
    def connect_cancel(platform: str):
        if (flow := youtube_connect.get("flow")) is not None:
            flow.cancel()
        return {"ok": True}

    @api.delete("/accounts/{account_id}")
    def delete_account(account_id: int, conn=Depends(get_conn)):
        row = conn.execute("SELECT keyring_ref FROM account WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "compte introuvable")
        if row["keyring_ref"]:
            secrets.delete(row["keyring_ref"])
        with db.tx(conn):
            conn.execute("DELETE FROM account WHERE id = ?", (account_id,))
        return {"ok": True}

    app.include_router(api)

    # -- interface web (construite par `npm run build` dans frontend/) -----------
    if (WEB_DIR / "index.html").is_file():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    else:
        @app.get("/", response_class=HTMLResponse)
        def no_ui():
            return ("<p style='font-family:sans-serif'>Interface non construite : "
                    "<code>npm --prefix frontend install && npm --prefix frontend run build</code></p>")

    return app

