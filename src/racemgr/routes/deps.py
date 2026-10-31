from __future__ import annotations

from collections.abc import Iterator
from functools import wraps
from typing import Any

from fastapi import Depends, Request
from sqlmodel import Session

from .. import auth
from ..models import Official
from ..services import ServiceError
from ..services.race import RaceService


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine, expire_on_commit=False) as s:
        yield s


def get_official(request: Request, session: Session = Depends(get_session)) -> Official | None:
    o = auth.official_from_request(session, request)
    request.state.official = o
    if o is not None:
        event_name, track_name = auth.bound_names(session, o)
        request.state.official_scope = auth.scope_label(o, track_name, event_name)
    return o


def public_cached(fn):
    """Serve anonymous viewers from the shared page cache (see cache.py).
    Officials (any device cookie) always get a fresh render. The route must
    take ``request`` and be a sync def."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        request: Request = kwargs["request"]
        if request.cookies.get(auth.COOKIE):
            return fn(*args, **kwargs)
        key = f"{request.url.path}?{request.url.query}"
        return request.app.state.page_cache.get(key, lambda: fn(*args, **kwargs))

    return wrapper


def race(request: Request) -> RaceService:
    return request.app.state.race


def role_context(request: Request) -> dict[str, Any]:
    """Who this device is, for the role bar and nav on every page."""
    if not hasattr(request.state, "official"):  # public routes don't depend on get_official
        with Session(request.app.state.engine, expire_on_commit=False) as s:
            get_official(request, s)
    me = request.state.official
    path = request.url.path
    nav = [
        {"href": href, "label": label, "current": path == href or (href not in ("/", "/operate") and path.startswith(href + "/"))}
        for href, label in auth.nav_for(me)
    ]
    ctx: dict[str, Any] = {"me": me, "nav": nav}
    if me is not None:
        home = auth.home_for(me)
        ctx.update(
            my_home=home,
            my_role=auth.ROLE_LABELS[me.role],
            my_scope=getattr(request.state, "official_scope", ""),
            my_hint=auth.ROLE_HINTS[me.role],
            # sub-pages count too: /admin/event/3 is still the admin's own area
            on_my_page=path == home
            or path.startswith(home.rstrip("/") + "/")
            or (me.role.value == "track_operator" and not me.track_id and path.startswith("/track/")),
        )
    return ctx


def render(request: Request, template: str, ctx: dict[str, Any] | None = None, status_code: int = 200):
    full = role_context(request)
    full.update(ctx or {})
    return request.app.state.templates.TemplateResponse(request, template, full, status_code=status_code)


def require(ok: bool, what: str = "this page") -> None:
    if not ok:
        raise ServiceError(f"This device can't open {what}.", 403)
