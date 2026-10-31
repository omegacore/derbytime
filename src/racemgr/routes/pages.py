"""HTML pages and the HTMX partials they refresh. One URL per screen."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, col, or_, select

from .. import auth
from ..models import (
    Entry,
    Event,
    EventStatus,
    EventType,
    Group,
    Heat,
    HeatLane,
    HeatStatus,
    Official,
    Racer,
    Round,
    RoundKind,
    Track,
)
from ..services import ServiceError, views
from ..services.race import RaceService
from ..services.schedule import event_standings, pack_standings
from .deps import get_official, get_session, public_cached, race, render, require

router = APIRouter()


def active_events(s: Session) -> list[Event]:
    return list(s.exec(select(Event).where(Event.status == EventStatus.active).order_by(Event.id)))


def get_track(s: Session, track_id: int) -> Track:
    t = s.get(Track, track_id)
    if not t:
        raise ServiceError("Track not found", 404)
    return t


def tracks_with_state(s: Session, rs: RaceService, events: list[Event]) -> list[dict]:
    out = []
    for ev in events:
        for t in s.exec(select(Track).where(Track.event_id == ev.id, Track.active == True).order_by(Track.id)):  # noqa: E712
            out.append({"event": ev, **views.track_state(s, t, rs.timer_status(t.id))})
    return out


def shared_standings(request: Request, s: Session, ev: Event) -> list[dict]:
    """event_standings, computed once per data change and shared by every
    racer page (each racer page used to recompute the whole event)."""
    return request.app.state.page_cache.get(f"standings:{ev.id}", lambda: event_standings(s, ev))


# --- public -------------------------------------------------------------------


@router.get("/")
def home(request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    return render(request, "home.html", {"events": active_events(s)})


@router.get("/partials/home")
@public_cached
def home_partial(request: Request, s: Session = Depends(get_session), rs: RaceService = Depends(race)):
    return render(request, "partials/home_tracks.html", {"tracks": tracks_with_state(s, rs, active_events(s))})


@router.get("/partials/search")
def search(request: Request, q: str = "", s: Session = Depends(get_session)):
    q = q.strip()
    rows = []
    if q:
        like = f"%{q}%"
        cond = or_(col(Racer.first_name).ilike(like), col(Racer.last_name).ilike(like))
        if q.isdigit():
            ids = s.exec(select(Entry.racer_id).where(Entry.number == int(q))).all()
            cond = or_(cond, col(Racer.id).in_(ids))
        rows = s.exec(
            select(Racer, Group).join(Group, Group.id == Racer.group_id, isouter=True).where(cond).order_by(Racer.first_name).limit(20)
        ).all()
    return render(request, "partials/search.html", {"rows": rows, "q": q})


@router.get("/event/{event_id}")
def event_page(event_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    ev = s.get(Event, event_id)
    if not ev:
        raise ServiceError("Event not found", 404)
    return render(request, "event.html", {"event": ev})


@router.get("/partials/event/{event_id}")
@public_cached
def event_partial(event_id: int, request: Request, s: Session = Depends(get_session)):
    ev = s.get(Event, event_id)
    if not ev:
        raise ServiceError("Event not found", 404)
    rounds = s.exec(select(Round).where(Round.event_id == ev.id).order_by(Round.seq, Round.id)).all()
    schedule = []
    for r in rounds:
        heats = s.exec(
            select(Heat).where(Heat.round_id == r.id, Heat.status != HeatStatus.rerun).order_by(Heat.seq)
        ).all()
        schedule.append({"round": r, "heats": views.heat_views(s, list(heats))})
    return render(
        request,
        "partials/event_body.html",
        {"event": ev, "standings": event_standings(s, ev), "schedule": schedule},
    )


@router.get("/racer/{racer_id}")
def racer_page(racer_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    racer = s.get(Racer, racer_id)
    if not racer:
        raise ServiceError("Racer not found", 404)
    group = s.get(Group, racer.group_id) if racer.group_id else None
    return render(request, "racer.html", {"racer": racer, "group": group})


@router.get("/partials/racer/{racer_id}")
@public_cached
def racer_partial(racer_id: int, request: Request, s: Session = Depends(get_session)):
    racer = s.get(Racer, racer_id)
    if not racer:
        raise ServiceError("Racer not found", 404)
    entries = s.exec(select(Entry, Event).join(Event, Event.id == Entry.event_id).where(Entry.racer_id == racer_id)).all()
    blocks = []
    for entry, ev in entries:
        heat_ids = s.exec(select(HeatLane.heat_id).where(HeatLane.entry_id == entry.id)).all()
        heats = s.exec(
            select(Heat).where(col(Heat.id).in_(heat_ids), Heat.status != HeatStatus.rerun).order_by(Heat.round_id, Heat.seq)
        ).all()
        hv = views.heat_views(s, list(heats))
        for h in hv:
            h["mine"] = next(l for l in h["lanes"] if l.get("entry_id") == entry.id)
        rank = None
        for block in shared_standings(request, s, ev):
            for row in block["rows"]:
                if row["entry_id"] == entry.id:
                    rank = (block["name"], row["s"].rank, len(block["rows"]))
        blocks.append({"entry": entry, "event": ev, "heats": hv, "rank": rank})
    return render(request, "partials/racer_body.html", {"racer": racer, "blocks": blocks})


# --- kiosk displays -------------------------------------------------------------


@router.get("/display/ondeck/{track_id}")
def ondeck(track_id: int, request: Request, s: Session = Depends(get_session)):
    t = get_track(s, track_id)
    return render(request, "display_ondeck.html", {"track": t, "kiosk": True})


@router.get("/partials/ondeck/{track_id}")
@public_cached
def ondeck_partial(track_id: int, request: Request, s: Session = Depends(get_session), rs: RaceService = Depends(race)):
    t = get_track(s, track_id)
    return render(request, "partials/ondeck.html", {"st": views.track_state(s, t, rs.timer_status(t.id), upcoming=2)})


# Board colours by rank; a den matches when its name contains the keyword.
RANK_THEMES = [
    ("lion", "lion"), ("tiger", "tiger"), ("wolf", "wolf"), ("bear", "bear"),
    ("webelos", "webelos"), ("arrow of light", "aol"), ("aol", "aol"), ("arrow", "aol"),
]


def rank_theme(name: str) -> str:
    low = name.lower()
    return next((theme for word, theme in RANK_THEMES if word in low), "pack")


RANK_ICON_DIR = Path(__file__).resolve().parent.parent / "static" / "ranks"


def rank_icons() -> dict[str, str]:
    """Official emblem files dropped into static/ranks/, by theme."""
    found = {}
    for ext in ("svg", "webp", "png"):  # first match wins
        for f in RANK_ICON_DIR.glob(f"*.{ext}"):
            found.setdefault(f.stem.lower(), f"/static/ranks/{f.name}")
    return found


def board_vehicle(ev: Event) -> str:
    """Progress-bar rider: by event name ("regatta"/"derby"), else by type."""
    name = ev.name.lower()
    if "regatta" in name:
        return "boat"
    if "derby" in name:
        return "car"
    return "boat" if ev.type == EventType.regatta else "car"


def board_slides(s: Session, events: list[Event], per_page: int) -> list[dict]:
    """One slide per den (paged when long), then finals, then the pack overall."""
    slides = []
    for ev in events:
        vehicle = board_vehicle(ev)
        blocks = event_standings(s, ev)
        themed = [(b, rank_theme(b["name"]) if b["round"].kind == RoundKind.group else "pack") for b in blocks]
        for b, theme in [*themed, (pack_standings(s, ev), "pack")]:
            key = f"r{b['round'].id}" if b["round"] else "pack"
            # Spread rows evenly over as few pages as fit (12 at 10/page -> 6 + 6).
            size = -(-len(b["rows"]) // -(-len(b["rows"]) // per_page)) if b["rows"] else per_page
            pages = [b["rows"][i : i + size] for i in range(0, len(b["rows"]), size)] or [[]]
            # The leader's vehicle is featured on every page of the block.
            lead = next((r for r in b["rows"] if r["s"].score is not None), None)
            for n, rows in enumerate(pages, start=1):
                slides.append({
                    "id": f"{ev.id}-{key}-{n}", "key": f"{ev.id}-{key}", "event": ev, "theme": theme, "vehicle": vehicle,
                    "block": {**b, "rows": rows}, "page": n, "pages": len(pages),
                    "lead": lead if lead and lead["photo_url"] else None,
                })
    return slides


@router.get("/display/standings")
def standings_display(request: Request, event: int | None = None, rows: int = 10, secs: int = 12):
    """Kiosk board. ``rows`` per slide, ``secs`` on each slide."""
    q = f"?rows={max(3, rows)}" + (f"&event={event}" if event else "")
    return render(request, "display_standings.html", {"query": q, "secs": max(3, secs), "kiosk": True})


@router.get("/partials/standings")
@public_cached
def standings_partial(request: Request, event: int | None = None, rows: int = 10, s: Session = Depends(get_session)):
    events = [s.get(Event, event)] if event else active_events(s)
    slides = board_slides(s, [e for e in events if e], max(3, rows))
    return render(request, "partials/standings.html", {"slides": slides, "icons": rank_icons()})


# --- officials -----------------------------------------------------------------


@router.get("/operate")
def operate_picker(request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    """Track list for operators who aren't bound to one track (and admins/timekeepers)."""
    events = [ev for ev in active_events(s) if not (me and me.event_id) or ev.id == me.event_id]
    tracks = [
        (ev, t)
        for ev in events
        for t in s.exec(select(Track).where(Track.event_id == ev.id, Track.active == True).order_by(Track.id))  # noqa: E712
        if auth.can_operate(me, t.id)
    ]
    # can_operate(me, -1) is true for anyone allowed on every track
    require(bool(tracks) or auth.can_operate(me, -1), "the operator pages")
    if len(tracks) == 1:
        return RedirectResponse(f"/track/{tracks[0][1].id}/operate", status_code=303)
    return render(request, "operate_picker.html", {"tracks": tracks})


@router.get("/track/{track_id}/operate")
def operate(track_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    require(auth.can_operate(me, track_id), "this track's operator page")
    t = get_track(s, track_id)
    return render(request, "operate.html", {"track": t})


@router.get("/partials/operate/{track_id}")
def operate_partial(track_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    require(auth.can_operate(me, track_id))
    t = get_track(s, track_id)
    st = views.track_state(s, t, rs.timer_status(t.id), upcoming=6)
    return render(request, "partials/operate_body.html", {"st": st})


@router.get("/timekeeper")
def timekeeper(request: Request, me=Depends(get_official)):
    require(auth.can_timekeep(me), "the timekeeper page")
    return render(request, "timekeeper.html")


@router.get("/partials/timekeeper")
def timekeeper_partial(request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    require(auth.can_timekeep(me))
    on_track = views.heat_views(s, views.on_track_heats(s))
    pending = rs.pending_heats()
    if me.event_id:
        on_track = [h for h in on_track if h["event_id"] == me.event_id]
        pending = [h for h in pending if h["event_id"] == me.event_id]
    return render(request, "partials/timekeeper_body.html", {"pending": pending, "on_track": on_track})


@router.get("/track/{track_id}/lane/{lane}")
def lane_page(track_id: int, lane: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    require(auth.can_time_lane(me, track_id, lane), "this lane's stopwatch")
    t = get_track(s, track_id)
    if not 1 <= lane <= t.lane_count:
        raise ServiceError("No such lane", 404)
    return render(request, "lane.html", {"track": t, "lane": lane})


@router.get("/partials/lane/{track_id}/{lane}")
def lane_partial(track_id: int, lane: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    require(auth.can_time_lane(me, track_id, lane))
    t = get_track(s, track_id)
    st = views.track_state(s, t, rs.timer_status(t.id), upcoming=1)
    cur = st["current"]
    my = next((l for l in cur["lanes"] if l["lane"] == lane), None) if cur else None
    nxt = st["upcoming"][0] if st["upcoming"] else None
    my_next = next((l for l in nxt["lanes"] if l["lane"] == lane), None) if nxt else None
    return render(request, "partials/lane_info.html", {"st": st, "cur": cur, "my": my, "lane": lane, "nxt": nxt, "my_next": my_next})


@router.get("/checkin")
def checkin(request: Request, event: int | None = None, s: Session = Depends(get_session), me=Depends(get_official)):
    require(auth.can_checkin(me), "check-in")
    if me.event_id:
        events = [e for e in [s.get(Event, me.event_id)] if e]
        ev = events[0] if events else None
    else:
        events = list(s.exec(select(Event).where(Event.status != EventStatus.done).order_by(Event.id)))
        ev = s.get(Event, event) if event else (events[0] if events else None)
    rows = []
    if ev:
        rows = s.exec(
            select(Entry, Racer, Group)
            .join(Racer, Racer.id == Entry.racer_id)
            .join(Group, Group.id == Racer.group_id, isouter=True)
            .where(Entry.event_id == ev.id)
            .order_by(Entry.number)
        ).all()
    return render(request, "checkin.html", {"events": events, "event": ev, "rows": rows})


# --- joining -------------------------------------------------------------------


@router.get("/join/{token}")
def join_page(token: str, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    from ..models import JoinToken

    jt = s.exec(select(JoinToken).where(JoinToken.token == token)).first()
    problem = auth.token_problem(jt)
    track = s.get(Track, jt.track_id) if jt and jt.track_id else None
    event = s.get(Event, jt.event_id) if jt and jt.event_id else None
    return render(request, "join.html", {"jt": jt, "problem": problem, "track": track, "event": event, "token": token}, 410 if problem else 200)


@router.post("/join/{token}")
def join(token: str, request: Request, s: Session = Depends(get_session), me: Official | None = Depends(get_official)):
    official, device = auth.redeem(s, token)
    if me is not None:
        # a phone has one role at a time; retire the old one so the admin's device list stays accurate
        me.revoked = True
        s.add(me)
    s.commit()
    resp = RedirectResponse(auth.home_for(official), status_code=303)
    days = request.app.state.settings.device_cookie_days
    resp.set_cookie(
        auth.COOKIE,
        device,
        max_age=int(timedelta(days=days).total_seconds()),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@router.get("/me")
def me_page(request: Request, s: Session = Depends(get_session), me: Official | None = Depends(get_official)):
    track = s.get(Track, me.track_id) if me and me.track_id else None
    event = s.get(Event, me.event_id) if me and me.event_id else None
    return render(request, "me.html", {"track": track, "event": event, "home": auth.home_for(me) if me else "/"})


@router.post("/logout")
def logout(request: Request, s: Session = Depends(get_session), me: Official | None = Depends(get_official)):
    if me:
        me.revoked = True
        s.add(me)
        s.commit()
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp
