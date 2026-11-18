"""Ligne de commande : `autoclip <commande>` (ou `python -m autoclip`)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from . import secrets, services
from .config import API_HOST, API_PORT, default_home


def _runtime(args):
    from .runtime import Runtime

    return Runtime.open(args.home)


def _fmt_time(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d/%m %H:%M") if ts else "-"


def _fmt_span(start: float, end: float) -> str:
    return f"{int(start // 60)}:{start % 60:04.1f} → {int(end // 60)}:{end % 60:04.1f}"


# -- commandes ------------------------------------------------------------------

def cmd_daemon(args) -> int:
    from .runtime import run_daemon

    run_daemon(args.home, host=args.host, port=args.port)
    return 0


def cmd_app(args) -> int:
    from .app import run_app

    return run_app(args.home, debug=args.debug)


def cmd_install_desktop(args) -> int:
    from .app import install_desktop_entry

    print(f"raccourci créé : {install_desktop_entry()}")
    return 0


def cmd_work(args) -> int:
    rt = _runtime(args)
    rt.ctx.queue.requeue_orphans()
    rt.scheduler.tick()
    print(f"{rt.worker.run_until_idle()} job(s) traité(s) ; file : {rt.ctx.queue.stats()}")
    return 0


def cmd_bot(args) -> int:
    rt = _runtime(args)
    conn = rt.ctx.conn
    if args.action == "list":
        for bot in conn.execute("SELECT * FROM bot ORDER BY id"):
            mode = "dry run" if bot["dry_run"] else "réel"
            print(f"#{bot['id']:<3} {bot['name']:<30} [{mode}] {services.bot_stats(conn, bot['id'])}")
    elif args.action == "create":
        settings = json.loads(args.settings) if args.settings else None
        bot_id = services.create_bot(conn, args.name, dry_run=not args.live, clip_settings=settings)
        print(f"bot #{bot_id} créé ({'publication réelle' if args.live else 'dry run'})")
    elif args.action == "rename":
        services.update_bot(conn, args.bot, name=args.name)
    elif args.action == "live":
        services.update_bot(conn, args.bot, dry_run=args.off)
        print("dry run" if args.off else "publication réelle activée")
    elif args.action == "delete":
        services.delete_bot(conn, args.bot)
    return 0


def cmd_source(args) -> int:
    rt = _runtime(args)
    source_id = services.add_source(rt.ctx.conn, rt.ctx.queue, args.bot, args.uri)
    print(f"source #{source_id} ajoutée ; le démon (ou `autoclip work`) va la traiter")
    return 0


def cmd_review(args) -> int:
    rt = _runtime(args)
    conn = rt.ctx.conn
    if args.action == "list":
        clips = services.list_clips(conn, bot_id=args.bot, status=args.status)
        if not clips:
            print("aucun clip")
        for c in clips:
            print(f"#{c['id']:<4} {c['status']:<10} score {c['score']:5.2f}  "
                  f"{_fmt_span(c['start_ms'] / 1000, c['end_ms'] / 1000)}  {c['title']}")
            if c["file_path"]:
                print(f"      {c['file_path']}")
    elif args.action == "approve":
        for clip_id in args.clips:
            n = services.approve_clip(conn, clip_id, title=args.title)
            print(f"clip #{clip_id} validé ({n} publication(s) planifiée(s))")
    elif args.action == "reject":
        for clip_id in args.clips:
            services.reject_clip(conn, clip_id)
            print(f"clip #{clip_id} rejeté")
    return 0


def cmd_schedule(args) -> int:
    rt = _runtime(args)
    services.set_schedule(rt.ctx.conn, args.bot, every_hours=args.every, days=args.days,
                          window=args.window, timezone=args.tz, enabled=not args.disable)
    print("planning enregistré")
    return 0


def cmd_queue(args) -> int:
    rt = _runtime(args)
    conn = rt.ctx.conn
    print("jobs :", rt.ctx.queue.stats())
    for j in conn.execute("SELECT * FROM job WHERE status IN ('queued', 'running', 'failed') ORDER BY id"):
        print(f"  job #{j['id']:<4} {j['kind']:<10} {j['status']:<8} essais {j['attempts']}/{j['max_attempts']}"
              f"  {(j['last_error'] or '')[:80]}")
    print("publications :")
    for p in conn.execute("SELECT * FROM publish_job ORDER BY COALESCE(scheduled_at, 1e18), id"):
        print(f"  clip #{p['clip_id']:<4} compte #{p['account_id']} {p['status']:<10} "
              f"{_fmt_time(p['scheduled_at'])}  {p['url'] or p['last_error'] or ''}")
    return 0


def cmd_settings(args) -> int:
    rt = _runtime(args)
    if args.set:
        changes = {}
        for item in args.set:
            key, _, raw = item.partition("=")
            try:
                changes[key] = json.loads(raw)
            except json.JSONDecodeError:
                changes[key] = raw
        services.update_settings(rt.ctx.conn, changes)
    print(json.dumps(services.get_settings(rt.ctx.conn), indent=2, ensure_ascii=False))
    return 0


def cmd_credentials(args) -> int:
    if args.action == "status":
        for platform, ok in secrets.credentials_status().items():
            print(f"{platform:<10} {'configuré' if ok else '-'}")
    elif args.action == "set":
        if args.platform == "youtube" and args.file:
            data = json.loads(Path(args.file).expanduser().read_text())
            values = secrets.youtube_credentials_from_client_secret(data)
        else:
            import getpass

            values = {f: getpass.getpass(f"{f} : ") for f in secrets.CREDENTIAL_FIELDS[args.platform]}
        secrets.set_credentials(args.platform, values)
        print(f"clés {args.platform} enregistrées dans le trousseau")
    elif args.action == "delete":
        secrets.delete(secrets.credentials_key(args.platform))
    elif args.action == "test":
        from . import keycheck

        platforms = [args.platform] if args.platform else [
            p for p, ok in secrets.credentials_status().items() if ok]
        failed = 0
        for platform in platforms:
            result = keycheck.check(platform)
            failed += not result.ok
            print(f"{platform:<13} {'OK ' if result.ok else 'ÉCHEC'}  {result.message}")
        return 1 if failed else 0
    return 0


def cmd_account(args) -> int:
    rt = _runtime(args)
    conn = rt.ctx.conn
    if args.action == "list":
        for a in conn.execute("SELECT * FROM account ORDER BY id"):
            print(f"#{a['id']:<3} {a['platform']:<10} {a['display_name']}")
    elif args.action == "connect":
        from .publishers.youtube import connect_account

        print(f"compte #{connect_account(conn)} connecté")
    elif args.action == "link":
        services.link_account(conn, args.bot, args.account)
    elif args.action == "unlink":
        services.unlink_account(conn, args.bot, args.account)
    return 0


def cmd_watch(args) -> int:
    rt = _runtime(args)
    conn = rt.ctx.conn
    if args.action == "add":
        watch_id = services.add_watch(conn, args.bot, args.platform, args.channel, args.every)
        print(f"surveillance #{watch_id} : {args.platform} {args.channel}, toutes les {args.every} min")
    elif args.action == "remove":
        services.remove_watch(conn, args.watch)
    else:
        for w in conn.execute("SELECT * FROM watch ORDER BY bot_id, id"):
            print(f"#{w['id']:<3} bot #{w['bot_id']:<3} {w['platform']:<8} {w['channel']:<30} "
                  f"toutes les {w['interval_minutes']} min · dernière vérif. {_fmt_time(w['last_checked_at'])}")
        for d in conn.execute("SELECT * FROM discovered ORDER BY id DESC LIMIT 15"):
            print(f"  {d['platform']} {d['external_id']:<14} {d['status']:<9} {d['signal'] or '-':<8} "
                  f"{d['title'][:50]}  {d['error'] or ''}")
    return 0


def cmd_ai(args) -> int:
    rt = _runtime(args)
    conn = rt.ctx.conn
    if args.action == "set":
        changes = {}
        for item in args.values:
            key, _, raw = item.partition("=")
            try:
                changes[key] = json.loads(raw)
            except json.JSONDecodeError:
                changes[key] = raw
        services.update_bot(conn, args.bot, ai_settings=changes)
    from .ai.budget import today, used_tokens

    bot = services.get_bot(conn, args.bot)
    settings = services.ai_settings(bot)
    print(json.dumps(settings, indent=2, ensure_ascii=False))
    print(f"aujourd'hui : {used_tokens(conn, args.bot, today())} / {settings['daily_token_budget']} tokens"
          + ("  (dry run : faux fournisseur, aucun appel payant)" if bot["dry_run"] else ""))
    return 0


def cmd_identity(args) -> int:
    from . import identity

    rt = _runtime(args)
    conn = rt.ctx.conn
    if args.action == "models":
        from .ai.providers import make_text_provider

        for name in make_text_provider(args.provider).list_models():
            print(name)
        return 0
    if args.action == "generate":
        ident_id = identity.start(conn, rt.ctx.queue, args.bot, args.brief or "")
        print(f"proposition {ident_id} en cours (traitée par le démon, ou `autoclip work`)")
        return 0
    current = identity.latest(conn, args.bot)
    if current is None:
        print("aucune proposition : `autoclip identity generate BOT --brief ...`")
        return 1
    if args.action == "apply":
        identity.apply(conn, rt.ctx.queue, current["id"])
        print("application en cours sur la chaîne (traitée par le démon, ou `autoclip work`)")
        return 0
    print(json.dumps({k: current[k] for k in ("id", "status", "images_status", "choice", "avatar_path",
                                               "banner_path", "error")}, indent=2, ensure_ascii=False))
    return 0


def cmd_alerts(args) -> int:
    rt = _runtime(args)
    alerts = services.list_alerts(rt.ctx.conn)
    if not alerts:
        print("aucune alerte")
    for a in alerts:
        print(f"{_fmt_time(a['created_at'])}  bot #{a['bot_id']}  {a['kind']}  {a['message']}")
    return 0


def cmd_twitch(args) -> int:
    from .discovery import twitch

    device = twitch.start_device_login()
    print(f"Ouvre {device['verification_uri']} et saisis le code : {device['user_code']}")
    print(f"connecté en tant que {twitch.finish_device_login(device)} (jeton dans le trousseau)")
    return 0


def cmd_discover(args) -> int:
    rt = _runtime(args)
    for w in rt.ctx.conn.execute("SELECT id FROM watch WHERE enabled = 1"):
        rt.ctx.queue.enqueue("discover", {"watch_id": w["id"]})
    print(f"{rt.worker.run_until_idle()} job(s) traité(s) ; file : {rt.ctx.queue.stats()}")
    return 0


def cmd_dry_run(args) -> int:
    from . import dryrun

    home = args.home or default_home() / "dry-runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"dry run de {args.source} (données : {home})")
    report = dryrun.run(args.source, home=Path(home), every_hours=args.every, days=args.days,
                        window=args.window, timezone=args.tz, approve=not args.stop_at_review,
                        max_clips=args.max_clips)
    print(f"source : {report.source_status}" + (f" ({report.source_error})" if report.source_error else ""))
    for c in report.clips:
        print(f"#{c['id']:<3} {c['status']:<10} score {c['score']:5.2f}  {_fmt_span(c['start'], c['end'])}"
              f"  « {c['title']} »")
        print(f"     publié (virtuel) : {_fmt_time(c['published_at'])}   {c['file'] or c['error']}")
    ok = report.ok if not args.stop_at_review else bool(report.clips)
    print("OK : dry run complet" if ok else "ÉCHEC du dry run")
    return 0 if ok else 1


# -- parseur ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autoclip", description="Clipping et publication de Shorts")
    p.add_argument("--home", type=Path, default=None, help="dossier de données (défaut : ~/.local/share/autoclip)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("daemon", help="lance le démon (worker + planificateur + API)")
    d.add_argument("--host", default=API_HOST)
    d.add_argument("--port", type=int, default=API_PORT)
    d.set_defaults(func=cmd_daemon)

    a = sub.add_parser("app", help="ouvre la fenêtre AutoClip (démarre le démon si besoin)")
    a.add_argument("--debug", action="store_true", help="active l'inspecteur web")
    a.set_defaults(func=cmd_app)
    sub.add_parser("install-desktop", help="ajoute AutoClip au lanceur d'applications").set_defaults(
        func=cmd_install_desktop)

    sub.add_parser("work", help="traite la file jusqu'au bout, sans démon").set_defaults(func=cmd_work)

    b = sub.add_parser("bot", help="gérer les bots").add_subparsers(dest="action", required=True)
    b.add_parser("list")
    c = b.add_parser("create")
    c.add_argument("name")
    c.add_argument("--live", action="store_true", help="publication réelle (défaut : dry run)")
    c.add_argument("--settings", help='JSON, ex. \'{"max_clips": 3, "max_seconds": 45}\'')
    r = b.add_parser("rename")
    r.add_argument("bot", type=int)
    r.add_argument("name")
    lv = b.add_parser("live", help="active la publication réelle (--off pour revenir au dry run)")
    lv.add_argument("bot", type=int)
    lv.add_argument("--off", action="store_true")
    dl = b.add_parser("delete")
    dl.add_argument("bot", type=int)
    for parser in b.choices.values():
        parser.set_defaults(func=cmd_bot)

    s = sub.add_parser("source", help="ajouter une vidéo (URL ou fichier) à un bot")
    s.add_argument("bot", type=int)
    s.add_argument("uri")
    s.set_defaults(func=cmd_source)

    rv = sub.add_parser("review", help="file d'approbation").add_subparsers(dest="action", required=True)
    ls = rv.add_parser("list")
    ls.add_argument("--bot", type=int)
    ls.add_argument("--status", default="review", help="review, scheduled, published... (défaut : review)")
    ap = rv.add_parser("approve")
    ap.add_argument("clips", type=int, nargs="+")
    ap.add_argument("--title")
    rj = rv.add_parser("reject")
    rj.add_argument("clips", type=int, nargs="+")
    for parser in rv.choices.values():
        parser.set_defaults(func=cmd_review)

    sc = sub.add_parser("schedule", help="planning de publication d'un bot")
    sc.add_argument("bot", type=int)
    sc.add_argument("--every", type=float, required=True, help="heures entre deux publications")
    sc.add_argument("--days", default="all", help="ex. mon,tue,wed (défaut : all)")
    sc.add_argument("--window", default="00:00-00:00", help="plage horaire, ex. 09:00-21:00")
    sc.add_argument("--tz", default="Europe/Paris")
    sc.add_argument("--disable", action="store_true")
    sc.set_defaults(func=cmd_schedule)

    sub.add_parser("queue", help="état des jobs et des publications").set_defaults(func=cmd_queue)

    st = sub.add_parser("settings", help="réglages globaux")
    st.add_argument("--set", nargs="*", metavar="CLÉ=VALEUR")
    st.set_defaults(func=cmd_settings)

    cr = sub.add_parser("credentials", help="clés d'API (trousseau système)").add_subparsers(dest="action", required=True)
    cr.add_parser("status")
    cs = cr.add_parser("set")
    cs.add_argument("platform", choices=list(secrets.CREDENTIAL_FIELDS))
    cs.add_argument("file", nargs="?", help="youtube : chemin du client_secret.json")
    cd = cr.add_parser("delete")
    cd.add_argument("platform", choices=list(secrets.CREDENTIAL_FIELDS))
    ct = cr.add_parser("test", help="vérifie les clés auprès de chaque plateforme")
    ct.add_argument("platform", nargs="?", choices=list(secrets.CREDENTIAL_FIELDS))
    for parser in cr.choices.values():
        parser.set_defaults(func=cmd_credentials)

    ac = sub.add_parser("account", help="comptes de publication").add_subparsers(dest="action", required=True)
    ac.add_parser("list")
    ac.add_parser("connect", help="connecter une chaîne YouTube (navigateur)")
    for name in ("link", "unlink"):
        lk = ac.add_parser(name)
        lk.add_argument("bot", type=int)
        lk.add_argument("account", type=int)
    for parser in ac.choices.values():
        parser.set_defaults(func=cmd_account)

    wt = sub.add_parser("watch", help="chaînes YouTube / Twitch surveillées").add_subparsers(dest="action", required=True)
    wa = wt.add_parser("add")
    wa.add_argument("bot", type=int)
    wa.add_argument("platform", choices=["youtube", "twitch"])
    wa.add_argument("channel", help="pseudo, URL ou identifiant de la chaîne")
    wa.add_argument("--every", type=int, default=60, help="minutes entre deux vérifications")
    wt.add_parser("list")
    wr = wt.add_parser("remove")
    wr.add_argument("watch", type=int)
    for parser in wt.choices.values():
        parser.set_defaults(func=cmd_watch)

    ai = sub.add_parser("ai", help="IA d'un bot : fournisseur, modèle, budget")
    ai_sub = ai.add_subparsers(dest="action", required=True)
    ai_show = ai_sub.add_parser("show")
    ai_show.add_argument("bot", type=int)
    ai_set = ai_sub.add_parser("set", help="ex. : ai set 1 provider=anthropic daily_token_budget=150000")
    ai_set.add_argument("bot", type=int)
    ai_set.add_argument("values", nargs="+", metavar="CLÉ=VALEUR")
    for parser in ai_sub.choices.values():
        parser.set_defaults(func=cmd_ai)

    idt = sub.add_parser("identity", help="agent identité de chaîne (nom, bannière, playlists)")
    id_sub = idt.add_subparsers(dest="action", required=True)
    ig = id_sub.add_parser("generate", help="propose une identité pour la chaîne du bot")
    ig.add_argument("bot", type=int)
    ig.add_argument("--brief", help="thème, ton, public visé...")
    for name in ("show", "apply"):
        id_sub.add_parser(name).add_argument("bot", type=int)
    id_sub.add_parser("models", help="modèles disponibles chez un fournisseur").add_argument("provider")
    idt.set_defaults(func=cmd_identity)

    sub.add_parser("alerts", help="alertes (budget IA atteint...)").set_defaults(func=cmd_alerts)
    sub.add_parser("twitch-login", help="connecter un compte Twitch (lecture du chat)").set_defaults(func=cmd_twitch)
    sub.add_parser("discover", help="vérifie tout de suite les chaînes surveillées").set_defaults(func=cmd_discover)

    dr = sub.add_parser("dry-run", help="exécute tout le circuit sur une vidéo, sans publier")
    dr.add_argument("source", help="URL ou fichier vidéo")
    dr.add_argument("--every", type=float, default=4)
    dr.add_argument("--days", default="all")
    dr.add_argument("--window", default="09:00-21:00")
    dr.add_argument("--tz", default="Europe/Paris")
    dr.add_argument("--max-clips", type=int)
    dr.add_argument("--stop-at-review", action="store_true", help="s'arrêter aux clips en attente de validation")
    dr.set_defaults(func=cmd_dry_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose or args.command in ("daemon", "app") else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except services.ServiceError as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
