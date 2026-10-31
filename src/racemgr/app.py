from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.engine import Engine
from sqlmodel import Session

from . import auth
from .bus import EventBus
from .cache import BumpOnWrite, PageCache
from .config import Settings
from .db import init_db, make_engine
from .services import ServiceError
from .services.race import RaceService
from .ws import WebSocketHub

log = logging.getLogger("racemgr")
HERE = Path(__file__).parent


def fmt_time(t: float | None, dnf: bool = False) -> str:
    if dnf:
        return "DNF"
    return "" if t is None else f"{t:.3f}"


def make_templates() -> Jinja2Templates:
    t = Jinja2Templates(directory=str(HERE / "templates"))
    t.env.filters["t"] = fmt_time
    t.env.globals["ROLE_LABELS"] = {r.value: label for r, label in auth.ROLE_LABELS.items()}
    return t


def create_app(settings: Settings, engine: Engine | None = None, print_admin_link: bool = True) -> FastAPI:
    engine = engine or make_engine(settings.db_path)
    init_db(engine)
    bus = EventBus()
    cache = PageCache()
    bus.on_publish = cache.bump
    hub = WebSocketHub(bus)
    race = RaceService(settings, engine, bus)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.bind_loop(asyncio.get_running_loop())
        hub.start()
        await race.sync_timers()
        if print_admin_link:
            with Session(engine) as s:
                link = auth.ensure_admin_link(s, settings)
            if link:
                bar = "=" * 72
                print(f"\n{bar}\n  No admin device yet. Open this one-time admin link:\n\n    {link}\n{bar}\n", flush=True)
        yield
        await race.stop()
        await bus.close()

    app = FastAPI(title="Scout Race Manager", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.engine = engine
    app.state.bus = bus
    app.state.hub = hub
    app.state.race = race
    app.state.page_cache = cache
    app.add_middleware(BumpOnWrite, cache=cache)
    app.state.templates = make_templates()
    from .services.views import vehicle_photo_url

    app.state.templates.env.globals["vehicle_photo"] = vehicle_photo_url
    app.state.templates.env.globals["rerun_reasons"] = lambda event_type: settings.type_defaults(event_type).rerun_reasons

    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        wants_json = request.headers.get("content-type", "").startswith("application/json")
        if request.headers.get("HX-Request") or request.url.path.startswith("/api/") or wants_json:
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        from .routes.deps import render

        title = "Not your page" if exc.status == 403 else "Sorry"
        return render(request, "message.html", {"title": title, "message": exc.message, "denied": exc.status == 403}, exc.status)

    from .routes import admin, api, pages, roster

    app.include_router(pages.router)
    app.include_router(api.router)
    app.include_router(admin.router)
    app.include_router(roster.router)

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        await hub.handle(ws)

    @app.get("/healthz", response_class=HTMLResponse)
    async def healthz():
        return "ok"

    return app
