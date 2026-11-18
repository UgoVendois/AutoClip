"""API HTTP locale (127.0.0.1) : routes sous /api, interface web servie à la racine."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, db, secrets, services
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


class SourceIn(BaseModel):
    uri: str


class ScheduleIn(BaseModel):
    every_hours: float
    days: str = "all"
    window: str = "00:00-00:00"
    timezone: str = "UTC"
    enabled: bool = True


class ApproveIn(BaseModel):
    title: str | None = None
    description: str | None = None


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
        bot["accounts"] = _rows(conn.execute(
            "SELECT a.id, a.platform, a.display_name FROM account a "
            "JOIN bot_account ba ON ba.account_id = a.id WHERE ba.bot_id = ? ORDER BY a.id", (bot_id,)))
        sched = conn.execute("SELECT * FROM schedule WHERE bot_id = ?", (bot_id,)).fetchone()
        bot["schedule"] = dict(sched) if sched else None
        bot["stats"] = services.bot_stats(conn, bot_id)
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

    @api.post("/clips/{clip_id}/reject")
    def reject(clip_id: int, conn=Depends(get_conn)):
        guard(services.reject_clip, conn, clip_id)
        return {"ok": True}

    @api.get("/publish-jobs")
    def publish_jobs(bot_id: int | None = None, conn=Depends(get_conn)):
        query = """SELECT pj.*, c.title, c.bot_id, a.display_name AS account_name, a.platform
                   FROM publish_job pj JOIN clip c ON c.id = pj.clip_id
                   JOIN account a ON a.id = pj.account_id"""
        params: list[Any] = []
        if bot_id is not None:
            query += " WHERE c.bot_id = ?"
            params.append(bot_id)
        return _rows(conn.execute(query + " ORDER BY COALESCE(pj.scheduled_at, 1e18), pj.id", params))

    # -- file de jobs -------------------------------------------------------------
    @api.get("/jobs")
    def jobs(status: str | None = None, limit: int = 100, conn=Depends(get_conn)):
        query, params = "SELECT * FROM job", []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        rows = _rows(conn.execute(query + " ORDER BY id DESC LIMIT ?", (*params, limit)))
        return {"stats": JobQueue(conn).stats(), "jobs": rows}

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

    @api.delete("/credentials/{platform}")
    def delete_credentials(platform: str):
        secrets.delete(secrets.credentials_key(platform))
        return {"ok": True}

    @api.get("/accounts")
    def accounts(conn=Depends(get_conn)):
        return _rows(conn.execute(
            "SELECT id, platform, display_name, keyring_ref IS NOT NULL AS connected, created_at "
            "FROM account ORDER BY id"))

    @api.post("/accounts/{platform}/connect")
    async def connect(platform: str):
        """Ouvre la connexion OAuth dans le navigateur et attend son retour."""
        if platform != "youtube":
            raise HTTPException(400, f"{platform} n'est pas encore pris en charge (youtube uniquement)")
        from .publishers.youtube import connect_account

        def _connect() -> int:
            conn = db.connect(paths.db)
            try:
                return connect_account(conn)
            finally:
                conn.close()

        try:
            return {"id": await run_in_threadpool(_connect)}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from exc

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

