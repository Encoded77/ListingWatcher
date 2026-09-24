"""Command line: python -m listingwatcher <run|serve|scan|digest|probe|classify|test-notify|stats>."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import __version__
from .config import env, load_config
from .pipeline import build_app, build_context, evaluate, run_all, run_digests
from .scheduler import ScanRunner, Service
from .settings import SettingsManager, live_config_path


def _setup_logging(level: str) -> None:
    for stream in (sys.stdout, sys.stderr):        # listing titles with emoji on a cp1252 console (Windows)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="listingwatcher", description=f"ListingWatcher {__version__} — veille d'annonces multi-profils")
    p.add_argument("--config", default=None, help="config.yaml à utiliser tel quel, sans copie éditable "
                                                     "(défaut : la copie éditable si elle existe, sinon $LISTINGWATCHER_CONFIG ou ./config.yaml)")
    p.add_argument("--db", default=None, help="fichier SQLite (défaut: $LISTINGWATCHER_DB ou data/listingwatcher.sqlite)")
    p.add_argument("--dry-run", action="store_true", help="n'envoie rien à ntfy, affiche les notifications")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    sub = p.add_subparsers(dest="cmd", required=True)

    def watch_arg(sp):
        sp.add_argument("--watch", default=None, help="limiter à une veille (clé de `watches` dans config.yaml)")

    sub.add_parser("run", help="service continu (scans planifiés + digest)")
    s = sub.add_parser("scan", help="un scan complet puis sortie (pour le planificateur DSM)")
    s.add_argument("--source", default=None, help="limiter à une source : leboncoin | ebay")
    watch_arg(s)
    d = sub.add_parser("digest", help="envoie le digest des annonces actives")
    d.add_argument("--force", action="store_true", help="même s'il a déjà été envoyé aujourd'hui")
    watch_arg(d)
    pr = sub.add_parser("probe", help="fetch + classification sans rien enregistrer ni notifier")
    pr.add_argument("--source", default=None)
    pr.add_argument("--all", action="store_true", help="afficher aussi les annonces rejetées")
    watch_arg(pr)
    c = sub.add_parser("classify", help="classer un titre d'annonce")
    c.add_argument("title")
    c.add_argument("--desc", default="")
    c.add_argument("--price", type=float, default=None, help="prix rendu pour simuler la décision")
    watch_arg(c)
    sub.add_parser("serve", help="interface web seule, sans planificateur (mode « scan par cron »)")
    sub.add_parser("test-notify", help="envoie une notification de test sur ntfy")
    sub.add_parser("stats", help="état de la base")

    args = p.parse_args(argv)
    _setup_logging(args.log_level)
    # The editable copy (web interface settings) is authoritative as soon as it exists; the service
    # creates it on first start from the image's config.yaml. --config bypasses all of that.
    image_path = args.config or os.environ.get("LISTINGWATCHER_CONFIG", "config.yaml")
    live_path = None if args.config else live_config_path(args.db)
    if live_path and args.cmd in ("run", "serve"):
        SettingsManager.ensure_live(live_path, image_path)
    config_path = live_path if live_path and os.path.exists(live_path) else image_path
    logging.getLogger("listingwatcher").info("config: %s", config_path)
    cfg = load_config(config_path)
    watch = getattr(args, "watch", None)

    if args.cmd == "classify":
        ctx = build_context(cfg, db_path=":memory:", dry_run=True, fetchers=[], watch=watch)
        from .models import Listing
        listing = Listing("cli", "0", "", args.title, args.price or 0.0, shipping=0.0, description=args.desc)
        info, decision = evaluate(ctx, listing)
        print(json.dumps({"watch": ctx.watch, "model": info.__dict__, "decision": decision.__dict__ if args.price else None},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    if args.cmd == "probe":
        app = build_app(cfg, db_path=":memory:", dry_run=True, only_source=args.source, only_watch=watch)
        if not any(ctx.fetchers for ctx in app.contexts):
            print("aucune source active (identifiants manquants ?)", file=sys.stderr)
            return 1
        for ctx in app.contexts:
            for f in ctx.fetchers:
                res = f.fetch()
                rows = []
                for l in res.listings:
                    info, dec = evaluate(ctx, l)
                    if dec.keep or args.all:
                        rows.append((dec.unit_price, dec.tier, l.status, info.label[:30], "/".join(info.flags or info.reasons)[:28],
                                     l.title[:60], l.url))
                rows.sort()
                print(f"\n== {ctx.watch}/{f.name}: {len(res.listings)} annonces, "
                      f"{sum(1 for r in rows if r[1] not in ('reject', 'ignore'))} retenues"
                      + ("" if res.complete else " (INCOMPLET)"))
                for r in rows:
                    print(f"{r[0]:7.0f} € {r[1]:8} {r[2]:8} {r[3]:30} {r[4]:28} {r[5]:60} {r[6]}")
                for e in res.errors:
                    print("  !", e)
        return 0

    app = build_app(cfg, db_path=args.db, dry_run=args.dry_run,
                    only_source=getattr(args, "source", None), only_watch=watch)

    def rebuild(new_cfg, old):
        new_cfg["_path"] = config_path
        same_transport = new_cfg.get("sources") == old.cfg.get("sources")
        return build_app(new_cfg, db_path=args.db, dry_run=args.dry_run,
                         transports=old.transports if same_transport else None)

    runner = ScanRunner(app, rebuild)
    settings = SettingsManager(config_path, image_path) if live_path and config_path == live_path else None
    try:
        if args.cmd == "run":
            Service(app, runner, settings).run()
        elif args.cmd == "serve":
            from .scheduler import build_web
            web = build_web(app, runner, settings)
            if web is None:
                print("web.enabled est à false dans la config", file=sys.stderr)
                return 1
            web.start()
            try:
                while True:
                    import time
                    time.sleep(60)
            except KeyboardInterrupt:
                web.stop()
        elif args.cmd == "scan":
            print(json.dumps(run_all(app), ensure_ascii=False))
        elif args.cmd == "digest":
            print("envoyé" if run_digests(app, force=args.force) else "rien à envoyer / déjà envoyé aujourd'hui / veille silencieuse")
        elif args.cmd == "test-notify":
            n = app.system_notifier
            ok = n.send("Veille annonces : test", f"ntfy fonctionne (topic {n.topic or '?'})", "default", ["white_check_mark"])
            print("ok" if ok else "échec")
            return 0 if ok else 1
        elif args.cmd == "stats":
            out = {"total": app.store.stats(), "watches": {c.watch: c.store.stats() for c in app.contexts}}
            print(json.dumps(out, ensure_ascii=False, indent=2))
    finally:
        runner.app.close()
    return 0
