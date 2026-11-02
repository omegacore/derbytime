"""`derbytime` command line."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from sqlmodel import Session, select

from . import __version__, auth
from .config import EXAMPLE_PATH, load_settings
from .db import init_db, make_engine


def cmd_serve(args, settings) -> int:
    import uvicorn

    from .app import create_app

    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        print("--ssl-certfile and --ssl-keyfile must be used together", file=sys.stderr)
        return 2
    settings.host = args.host or settings.host
    settings.port = args.port or settings.port
    settings.use_https = bool(args.ssl_certfile)
    app = create_app(settings)
    scheme = "https" if settings.use_https else "http"
    print(f"Derby Time {__version__} on {scheme}://{auth.lan_ip()}:{settings.port}  (db: {settings.db_path})", flush=True)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
        log_level=args.log_level,
        ws_ping_interval=20,
    )
    return 0


def cmd_init_db(args, settings) -> int:
    engine = make_engine(settings.db_path)
    init_db(engine)
    print(f"Database ready at {settings.db_path}")
    return 0


def cmd_seed_demo(args, settings) -> int:
    from .models import Event
    from .seed import seed_demo

    engine = make_engine(settings.db_path)
    init_db(engine)
    with Session(engine, expire_on_commit=False) as s:
        if s.exec(select(Event).where(Event.name.startswith("Demo "))).first() and not args.force:
            print("Demo events already exist (use --force to add another set).")
            return 1
        derby, regatta = seed_demo(s, settings)
    print(f"Created '{derby.name}' (event {derby.id}) and '{regatta.name}' (event {regatta.id}) with 20 racers.")
    return 0


def cmd_admin_url(args, settings) -> int:
    engine = make_engine(settings.db_path)
    init_db(engine)
    if args.port:
        settings.port = args.port
    settings.use_https = args.https
    with Session(engine) as s:
        link = auth.ensure_admin_link(s, settings, force=True)
    print(f"One-time admin link (valid {settings.admin_link_minutes} min):\n  {link}")
    return 0


def cmd_backup(args, settings) -> int:
    from datetime import datetime

    from .services.export import backup_db

    engine = make_engine(settings.db_path)
    dest = Path(args.path) if args.path else Path(settings.data_dir) / "backups" / f"derbytime-{datetime.now():%Y%m%d-%H%M%S}.db"
    if dest.exists() and not args.force:
        print(f"{dest} already exists (use --force to overwrite).")
        return 1
    dest.unlink(missing_ok=True)
    backup_db(engine, dest)
    print(f"Backed up {settings.db_path} to {dest}")
    return 0


def cmd_init_config(args, settings) -> int:
    dest = Path(args.path)
    if dest.exists() and not args.force:
        print(f"{dest} already exists (use --force to overwrite).")
        return 1
    shutil.copy(EXAMPLE_PATH, dest)
    print(f"Wrote {dest}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="derbytime", description="Derby Time")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", help="path to config.toml (default: $DERBYTIME_CONFIG, else ./config.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web server")
    s.add_argument("--host", help="bind address (default from config, 0.0.0.0)")
    s.add_argument("--port", type=int, help="port (default from config, 8000)")
    s.add_argument("--ssl-certfile", help="TLS certificate (e.g. from mkcert) for HTTPS")
    s.add_argument("--ssl-keyfile", help="TLS private key")
    s.add_argument("--log-level", default="info")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("init-db", help="create database tables")
    s.set_defaults(fn=cmd_init_db)

    s = sub.add_parser("seed-demo", help="create a demo derby and regatta")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_seed_demo)

    s = sub.add_parser("admin-url", help="print a fresh one-time admin link")
    s.add_argument("--port", type=int)
    s.add_argument("--https", action="store_true", help="link uses https://")
    s.set_defaults(fn=cmd_admin_url)

    s = sub.add_parser("backup", help="copy the database (safe while the server runs)")
    s.add_argument("path", nargs="?", help="destination file (default: <data_dir>/backups/derbytime-<time>.db)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("init-config", help="write an example config.toml")
    s.add_argument("path", nargs="?", default="config.toml")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init_config)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config) if args.cmd != "init-config" else None
    return args.fn(args, settings) or 0


if __name__ == "__main__":
    sys.exit(main())
