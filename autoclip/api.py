"""API HTTP locale (127.0.0.1) pilotée par la future interface (phase 2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from . import db, secrets, services
from .config import Paths
from .jobs.queue import JobQueue


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
    app = FastAPI(title="AutoClip", version="0.1.0")

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

    # -- bots -------------------------------------------------------------------
    @app.get("/bots")
    def list_bots(conn=Depends(get_conn)):
        bots = _rows(conn.execute("SELECT * FROM bot ORDER BY id"))
        for bot in bots:
            bot["stats"] = services.bot_stats(conn, bot["id"])
        return bots

    @app.post("/bots", status_code=201)
    def create_bot(body: BotIn, conn=Depends(get_conn)):
        return {"id": guard(services.create_bot, conn, body.name, dry_run=body.dry_run,
                            clip_settings=body.clip_settings)}

    @app.get("/bots/{bot_id}")
    def get_bot(bot_id: int, conn=Depends(get_conn)):
        bot = dict(guard(services.get_bot, conn, bot_id))
        bot["clip_settings"] = services.bot_settings(services.get_bot(conn, bot_id))
        bot["sources"] = _rows(conn.execute("SELECT * FROM source WHERE bot_id = ?", (bot_id,)))
        bot["accounts"] = _rows(conn.execute(
            "SELECT a.id, a.platform, a.display_name FROM account a "
            "JOIN bot_account ba ON ba.account_id = a.id WHERE ba.bot_id = ?", (bot_id,)))
        sched = conn.execute("SELECT * FROM schedule WHERE bot_id = ?", (bot_id,)).fetchone()
        bot["schedule"] = dict(sched) if sched else None
        bot["stats"] = services.bot_stats(conn, bot_id)
        return bot

    @app.patch("/bots/{bot_id}")
    def patch_bot(bot_id: int, body: BotPatch, conn=Depends(get_conn)):
        guard(services.update_bot, conn, bot_id, **body.model_dump())
        return {"ok": True}

    @app.delete("/bots/{bot_id}")
    def delete_bot(bot_id: int, conn=Depends(get_conn)):
        guard(services.delete_bot, conn, bot_id)
        return {"ok": True}

    @app.post("/bots/{bot_id}/sources", status_code=201)
    def add_source(bot_id: int, body: SourceIn, conn=Depends(get_conn)):
        return {"id": guard(services.add_source, conn, JobQueue(conn), bot_id, body.uri)}

    @app.put("/bots/{bot_id}/schedule")
    def put_schedule(bot_id: int, body: ScheduleIn, conn=Depends(get_conn)):
        guard(services.set_schedule, conn, bot_id, **body.model_dump())
        return {"ok": True}

    @app.post("/bots/{bot_id}/accounts/{account_id}")
    def link(bot_id: int, account_id: int, conn=Depends(get_conn)):
        guard(services.link_account, conn, bot_id, account_id)
        return {"ok": True}

    @app.delete("/bots/{bot_id}/accounts/{account_id}")
    def unlink(bot_id: int, account_id: int, conn=Depends(get_conn)):
        services.unlink_account(conn, bot_id, account_id)
        return {"ok": True}

    # -- clips et file d'approbation -------------------------------------------
    @app.get("/clips")
    def clips(bot_id: int | None = None, status: str | None = None, conn=Depends(get_conn)):
        rows = _rows(services.list_clips(conn, bot_id=bot_id, status=status))
        for r in rows:
            r.pop("words", None)
        return rows

    @app.post("/clips/{clip_id}/approve")
    def approve(clip_id: int, body: ApproveIn | None = None, conn=Depends(get_conn)):
        body = body or ApproveIn()
        return {"publish_jobs": guard(services.approve_clip, conn, clip_id, title=body.title,
                                      description=body.description)}

    @app.post("/clips/{clip_id}/reject")
    def reject(clip_id: int, conn=Depends(get_conn)):
        guard(services.reject_clip, conn, clip_id)
        return {"ok": True}

    @app.get("/publish-jobs")
    def publish_jobs(conn=Depends(get_conn)):
        return _rows(conn.execute("SELECT * FROM publish_job ORDER BY COALESCE(scheduled_at, 1e18), id"))

    # -- file de jobs -------------------------------------------------------------
    @app.get("/jobs")
    def jobs(status: str | None = None, limit: int = 100, conn=Depends(get_conn)):
        query, params = "SELECT * FROM job", []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        rows = _rows(conn.execute(query + " ORDER BY id DESC LIMIT ?", (*params, limit)))
        return {"stats": JobQueue(conn).stats(), "jobs": rows}

    @app.post("/jobs/{job_id}/retry")
    def retry(job_id: int, conn=Depends(get_conn)):
        if not JobQueue(conn).retry(job_id):
            raise HTTPException(400, "job introuvable ou pas en échec")
        return {"ok": True}

    # -- réglages, clés d'API, comptes ------------------------------------------
    @app.get("/settings")
    def get_settings(conn=Depends(get_conn)):
        return services.get_settings(conn)

    @app.put("/settings")
    def put_settings(changes: dict[str, Any], conn=Depends(get_conn)):
        return guard(services.update_settings, conn, changes)

    @app.get("/credentials")
    def credentials():
        """Quelles clés sont enregistrées (jamais leur valeur)."""
        return {"fields": secrets.CREDENTIAL_FIELDS, "configured": secrets.credentials_status()}

    @app.put("/credentials/{platform}")
    def put_credentials(platform: str, values: dict[str, str]):
        try:
            if platform == "youtube" and ("installed" in values or "web" in values):
                values = secrets.youtube_credentials_from_client_secret(values)
            secrets.set_credentials(platform, values)
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.delete("/credentials/{platform}")
    def delete_credentials(platform: str):
        secrets.delete(secrets.credentials_key(platform))
        return {"ok": True}

    @app.get("/accounts")
    def accounts(conn=Depends(get_conn)):
        return _rows(conn.execute("SELECT id, platform, display_name, created_at FROM account ORDER BY id"))

    @app.post("/accounts/{platform}/connect")
    async def connect(platform: str):
        """Ouvre la connexion OAuth dans le navigateur et attend son retour."""
        if platform != "youtube":
            raise HTTPException(400, f"{platform} n'est pas encore pris en charge (phase 1 : youtube)")
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

    @app.delete("/accounts/{account_id}")
    def delete_account(account_id: int, conn=Depends(get_conn)):
        row = conn.execute("SELECT keyring_ref FROM account WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "compte introuvable")
        if row["keyring_ref"]:
            secrets.delete(row["keyring_ref"])
        with db.tx(conn):
            conn.execute("DELETE FROM account WHERE id = ?", (account_id,))
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"ok": True}

    return app
