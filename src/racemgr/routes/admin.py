"""Admin screens: events, tracks, roster, schedule, officials/QR codes, audit log.

Plain HTML forms that POST and redirect back with a message (no HTMX needed).
"""
from __future__ import annotations

from urllib.parse import quote

import segno
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, col, func, select

from .. import auth
from ..models import (
    AuditLog,
    Entry,
    Event,
    EventStatus,
    EventType,
    Group,
    Heat,
    JoinToken,
    Official,
    Racer,
    Role,
    Round,
    ScoringMode,
    Track,
    utcnow,
)
from ..services import ServiceError
from ..services.audit import audit
from ..services.race import RaceService
from ..services.schedule import (
    add_pass,
    clear_schedule,
    create_final,
    create_runoff,
    den_entries,
    den_name,
    event_standings,
    MAX_RACES,
    generate_group_rounds,
    group_rounds,
    heat_editor,
    projected_finish,
    race_order,
    regenerate,
    remove_pass,
    save_heat_lineups,
    schedule_problems,
    set_race_order,
)
from ..services.setup import add_track, create_event
from ..core.scheduler import natural_key
from ..timers import REGISTRY
from .deps import get_official, get_session, race, render, require

router = APIRouter(prefix="/admin")


def back(url: str, msg: str = "", error: bool = False) -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + ("err=" if error else "msg=") + quote(msg)
    return RedirectResponse(url, status_code=303)


async def form(request: Request) -> dict:
    return dict(await request.form())


def admin_only(me: Official | None) -> None:
    require(auth.can_admin(me), "the admin pages")


def get_event(s: Session, event_id: int) -> Event:
    ev = s.get(Event, event_id)
    if not ev:
        raise ServiceError("Event not found", 404)
    return ev


@router.get("")
def admin_home(request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    events = s.exec(select(Event).order_by(col(Event.id).desc())).all()
    counts = dict(s.exec(select(Entry.event_id, func.count()).group_by(Entry.event_id)).all())
    return render(request, "admin/home.html", {"events": events, "counts": counts, "types": list(EventType), "modes": list(ScoringMode)})


@router.post("/events")
async def admin_create_event(request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    f = await form(request)
    try:
        ev = create_event(s, request.app.state.settings, f.get("name", ""), f.get("type", "derby"), auth.actor_for(me), f.get("scoring_mode") or None, f.get("event_date") or None)
        add_track(s, request.app.state.settings, ev, "Track 1" if ev.type == EventType.derby else "Gutter A", auth.actor_for(me))
        s.commit()
    except ServiceError as e:
        return back("/admin", e.message, True)
    return back(f"/admin/event/{ev.id}", "Event created with one default track.")


@router.get("/event/{event_id}")
def admin_event(event_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    ev = get_event(s, event_id)
    tracks = s.exec(select(Track).where(Track.event_id == ev.id).order_by(Track.id)).all()
    entries = s.exec(
        select(Entry, Racer, Group)
        .join(Racer, Racer.id == Entry.racer_id)
        .join(Group, Group.id == Racer.group_id, isouter=True)
        .where(Entry.event_id == ev.id)
        .order_by(Entry.number)
    ).all()
    heat_counts = dict(s.exec(select(Heat.track_id, func.count()).group_by(Heat.track_id)).all())
    rounds = s.exec(select(Round).where(Round.event_id == ev.id)).all()
    td = request.app.state.settings.type_defaults(ev.type.value)
    return render(
        request,
        "admin/event.html",
        {
            "event": ev,
            "tracks": tracks,
            "entries": entries,
            "out_count": sum(1 for e, r, _ in entries if e.status or not r.active),
            "heat_counts": heat_counts,
            "has_schedule": bool(rounds),
            "standings": event_standings(s, ev) if rounds else [],
            "order": race_order(s, ev) if rounds else [],
            "problems": schedule_problems(s, ev) if rounds else {},
            "projection": projected_finish(s, ev, td.heat_seconds + td.heat_overhead_seconds) if rounds else None,
            "dens": sorted(((gid, den_name(s, gid)) for gid in den_entries(s, ev.id)), key=lambda d: natural_key(d[1])),
            "default_passes": td.passes_per_den,
            "default_races": max((len(t.usable_lanes) for t in tracks if t.active), default=td.lane_count),
            "max_races": MAX_RACES,
            "multi_track": sum(1 for t in tracks if t.active) > 1,
            "sources": REGISTRY,
            "modes": list(ScoringMode),
            "statuses": list(EventStatus),
        },
    )


@router.post("/event/{event_id}/settings")
async def admin_event_settings(event_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    admin_only(me)
    ev = get_event(s, event_id)
    f = await form(request)
    url = f"/admin/event/{ev.id}"
    before = {"name": ev.name, "status": ev.status.value, "scoring_mode": ev.scoring_mode.value}
    try:
        ev.name = f.get("name", ev.name).strip() or ev.name
        ev.scoring_mode = ScoringMode(f.get("scoring_mode", ev.scoring_mode.value))
        ev.status = EventStatus(f.get("status", ev.status.value))
        ev.finalists_per_group = max(1, int(f.get("finalists_per_group", ev.finalists_per_group)))
        ev.dnf_time = float(f.get("dnf_time", ev.dnf_time))
    except ValueError:
        return back(url, "Invalid value.", True)
    s.add(ev)
    audit(s, auth.actor_for(me), "event.update", "event", ev.id, before=before, after={"name": ev.name, "status": ev.status.value, "scoring_mode": ev.scoring_mode.value})
    s.commit()
    await rs.sync_timers()
    return back(url, "Saved.")


@router.post("/event/{event_id}/tracks")
async def admin_add_track(event_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    admin_only(me)
    ev = get_event(s, event_id)
    f = await form(request)
    try:
        t = add_track(s, request.app.state.settings, ev, f.get("name", "Track"), auth.actor_for(me), int(f.get("lane_count") or 0) or None, f.get("timing_source") or None)
        s.commit()
    except (ServiceError, ValueError) as e:
        return back(f"/admin/event/{ev.id}", getattr(e, "message", str(e)), True)
    note = _reschedule(s, ev, me, f"track added: {t.name}")
    await rs.sync_timers()
    return back(f"/admin/event/{ev.id}", "Track added." + note)


def _reschedule(s: Session, ev: Event, me, reason: str) -> str:
    """After a capacity change, rebuild unraced heats if the event has a schedule."""
    if not s.exec(select(Round).where(Round.event_id == ev.id)).first():
        return ""
    try:
        r = regenerate(s, ev, auth.actor_for(me), reason)
        s.commit()
    except ServiceError as e:
        s.rollback()
        return f" The schedule was not rebuilt: {e.message}"
    return f" Unraced heats were rescheduled ({r['added']} queued, {r['kept']} raced heats kept)."


@router.post("/track/{track_id}")
async def admin_update_track(track_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    admin_only(me)
    t = s.get(Track, track_id)
    if not t:
        raise ServiceError("Track not found", 404)
    raw = await request.form()
    f = dict(raw)
    url = f"/admin/event/{t.event_id}"
    before = t.model_dump(exclude={"id"})
    capacity_before = (t.active, t.usable_lanes)
    try:
        lanes = int(f.get("lane_count", t.lane_count))
        if not 1 <= lanes <= 12:
            return back(url, "Lane count must be between 1 and 12.", True)
        if f.get("timing_source") not in REGISTRY:
            return back(url, "Unknown timing source.", True)
        t.name = f.get("name", t.name).strip() or t.name
        t.lane_count = lanes
        t.timing_source = f["timing_source"]
        t.min_time = float(f.get("min_time", t.min_time))
        t.max_time = float(f.get("max_time", t.max_time))
        t.active = f.get("active") == "on"
        if "lanes_shown" in f:  # the lane checkboxes were on the form
            on = {int(x) for x in raw.getlist("lane_on")}
            t.disabled_lanes = [l for l in range(1, lanes + 1) if l not in on] or None
        port = (f.get("port") or "").strip()
        t.timer_settings = {**(t.timer_settings or {}), "port": port} if port else {k: v for k, v in (t.timer_settings or {}).items() if k != "port"}
    except ValueError:
        return back(url, "Invalid number.", True)
    s.add(t)
    audit(s, auth.actor_for(me), "track.update", "track", t.id, before=before, after=t.model_dump(exclude={"id"}))
    s.commit()
    note = ""
    if (t.active, t.usable_lanes) != capacity_before:
        off = ", ".join(str(l) for l in (t.disabled_lanes or []))
        note = _reschedule(s, get_event(s, t.event_id), me, f"{t.name}: {'lanes ' + off + ' out' if off else 'all lanes in use'}{'' if t.active else ', track off'}")
    await rs.sync_timers()
    return back(url, f"{t.name} saved." + note)


@router.post("/event/{event_id}/regenerate")
async def admin_regenerate(event_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    admin_only(me)
    ev = get_event(s, event_id)
    f = await form(request)
    url = f"/admin/event/{ev.id}"
    try:
        r = regenerate(s, ev, auth.actor_for(me), (f.get("reason") or "").strip())
        s.commit()
    except ServiceError as e:
        s.rollback()
        return back(url, e.message, True)
    await rs.sync_timers()
    msg = f"Schedule rebuilt: {r['kept']} raced heats kept, {r['added']} heats queued."
    if r["new_dens"]:
        msg += f" Added runs for {r['new_dens']} new den(s)."
    return back(url, msg)


async def _schedule_action(request, event_id, s, me, rs, fn, ok_msg):
    admin_only(me)
    ev = get_event(s, event_id)
    url = f"/admin/event/{ev.id}"
    try:
        await fn(ev)
        s.commit()
    except ServiceError as e:
        s.rollback()
        return back(url, e.message, True)
    await rs.sync_timers()
    return back(url, ok_msg)


@router.post("/event/{event_id}/schedule")
async def admin_schedule(event_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    f = await form(request)

    async def fn(ev):
        try:
            passes = int(f.get("passes") or request.app.state.settings.type_defaults(ev.type.value).passes_per_den)
        except ValueError:
            raise ServiceError("Runs per den must be a number.")
        try:
            races = int(f["races"]) if f.get("races") else None
        except ValueError:
            raise ServiceError("Races per scout must be a number.")
        generate_group_rounds(s, ev, auth.actor_for(me), passes, races)
    return await _schedule_action(request, event_id, s, me, rs, fn, "Schedule generated. The event is now active.")


@router.post("/event/{event_id}/order")
async def admin_order(event_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    """Save a new race order (JSON from the drag-and-drop list)."""
    admin_only(me)
    ev = get_event(s, event_id)
    body = await request.json()
    try:
        ids = [int(x) for x in body.get("round_ids", [])]
    except (TypeError, ValueError):
        raise ServiceError("Bad round list.")
    set_race_order(s, ev, ids, auth.actor_for(me))
    s.commit()
    await rs.sync_timers()
    rs.publish_tracks()
    return {"ok": True}


@router.post("/event/{event_id}/reverse")
async def admin_reverse(event_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    async def fn(ev):
        ids = [r.id for r in group_rounds(s, ev.id)]
        set_race_order(s, ev, list(reversed(ids)), auth.actor_for(me))
    return await _schedule_action(request, event_id, s, me, rs, fn, "Race order reversed.")


@router.post("/event/{event_id}/passes")
async def admin_add_pass(event_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    f = await form(request)

    async def fn(ev):
        gid = f.get("group_id")
        add_pass(s, ev, int(gid) if gid else None, auth.actor_for(me))
    return await _schedule_action(request, event_id, s, me, rs, fn, "Run added to the end of the race order.")


@router.post("/round/{round_id}/remove")
async def admin_remove_pass(round_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    rnd = get_round(s, round_id)

    async def fn(ev):
        remove_pass(s, rnd, auth.actor_for(me))
    return await _schedule_action(request, rnd.event_id, s, me, rs, fn, "Run removed.")


def get_round(s: Session, round_id: int) -> Round:
    rnd = s.get(Round, round_id)
    if not rnd:
        raise ServiceError("Round not found", 404)
    return rnd


@router.get("/round/{round_id}/heats")
def admin_heats(round_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    """Hand-edit a run's unraced heats (e.g. to fix a heat with one car)."""
    admin_only(me)
    rnd = get_round(s, round_id)
    ev = get_event(s, rnd.event_id)
    return render(request, "admin/heats.html", {"event": ev, "round": rnd, **heat_editor(s, ev, rnd)})


@router.post("/round/{round_id}/heats")
async def admin_save_heats(round_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    admin_only(me)
    rnd = get_round(s, round_id)
    ev = get_event(s, rnd.event_id)
    raw = await request.form()
    url = f"/admin/round/{rnd.id}/heats"
    rows = []
    try:
        # one "row" field per heat, in the new queue order; its lanes are "lane-<row>-<lane>"
        for row in raw.getlist("row"):
            key, _, heat = str(row).partition(":")
            prefix = f"lane-{key}-"
            lineup = {int(k[len(prefix):]): int(v) if v else None for k, v in raw.multi_items() if k.startswith(prefix)}
            rows.append((heat, lineup))
    except ValueError:
        return back(url, "Invalid form.", True)
    try:
        r = save_heat_lineups(s, ev, rnd, str(raw.get("version", "")), rows, auth.actor_for(me))
        s.commit()
    except ServiceError as e:
        s.rollback()
        return back(url, e.message, True)
    await rs.sync_timers()
    msg = f"Heats saved: {r['heats']} to race."
    if r["added"]:
        msg += f" {r['added']} added."
    if r["removed"]:
        msg += f" {r['removed']} emptied and removed."
    return back(url, msg)


@router.post("/event/{event_id}/clear")
async def admin_clear(event_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    async def fn(ev):
        clear_schedule(s, ev, auth.actor_for(me))
    return await _schedule_action(request, event_id, s, me, rs, fn, "Schedule cleared.")


@router.post("/event/{event_id}/final")
async def admin_final(event_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    f = await form(request)

    async def fn(ev):
        create_final(s, ev, int(f["track_id"]) if f.get("track_id") else None, auth.actor_for(me))
    return await _schedule_action(request, event_id, s, me, rs, fn, "Final round created.")


@router.post("/event/{event_id}/runoff")
async def admin_runoff(event_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    f = await form(request)

    async def fn(ev):
        parent = s.get(Round, int(f["round_id"]))
        if not parent or parent.event_id != ev.id:
            raise ServiceError("Round not found", 404)
        ids = [int(x) for x in str(f.get("entry_ids", "")).split(",") if x]
        create_runoff(s, ev, parent, ids, int(f["track_id"]) if f.get("track_id") else None, auth.actor_for(me))
    return await _schedule_action(request, event_id, s, me, rs, fn, "Runoff added to the queue.")


# --- officials & QR codes -------------------------------------------------------------


@router.get("/officials")
def admin_officials(request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    now = utcnow()
    tokens = s.exec(
        select(JoinToken).where(JoinToken.revoked == False, JoinToken.expires_at > now).order_by(col(JoinToken.id).desc())  # noqa: E712
    ).all()
    tokens = [t for t in tokens if t.uses < t.max_uses]
    base = auth.base_url(request.app.state.settings, request)
    tracks = {t.id: t for t in s.exec(select(Track).order_by(Track.id))}
    events = {e.id: e for e in s.exec(select(Event).order_by(Event.id))}
    # finished events can't take new officials, and their tracks drop out of the picker
    open_events = [e for e in events.values() if e.status != EventStatus.done]
    open_events.sort(key=lambda e: e.status != EventStatus.active)  # active first: the default choice
    codes = []
    for t in tokens:
        url = f"{base}/join/{t.token}"
        svg = segno.make(url, error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff")
        codes.append({"t": t, "url": url, "svg": svg, "track": tracks.get(t.track_id), "event": events.get(t.event_id)})
    officials = s.exec(select(Official).where(Official.revoked == False).order_by(Official.event_id, Official.role, Official.id)).all()  # noqa: E712
    return render(
        request,
        "admin/officials.html",
        {
            "codes": codes,
            "officials": officials,
            "tracks": tracks,
            "events": events,
            "open_events": open_events,
            "roles": list(Role),
            "base": base,
            "now": now,
        },
    )


@router.post("/tokens")
async def admin_create_token(request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    f = await form(request)
    settings = request.app.state.settings
    try:
        role = Role(f.get("role"))
        track_id = int(f["track_id"]) if f.get("track_id") else None
        lane = int(f["lane"]) if f.get("lane") else None
        event_id = None
        if role != Role.admin:
            if f.get("event_id"):
                ev = s.get(Event, int(f["event_id"]))
            else:  # a track implies its event
                t = s.get(Track, track_id) if track_id else None
                ev = s.get(Event, t.event_id) if t else None
            if ev is None:
                raise ServiceError("Choose the event this official will work.")
            if ev.status == EventStatus.done:
                raise ServiceError(f"{ev.name} is finished. Choose an event that hasn't ended.")
            event_id = ev.id
        else:
            track_id = lane = None  # admins run every event
        if role == Role.lane_official and f.get("all_lanes") == "on" and track_id:
            t = s.get(Track, track_id)
            for n in range(1, t.lane_count + 1):
                auth.create_join_token(s, role, settings.join_token_minutes, track_id, n, f"{t.name} lane {n}", event_id=event_id)
        else:
            label = (f.get("label") or "").strip()
            auth.create_join_token(
                s, role, settings.join_token_minutes, track_id, lane, label, int(f.get("max_uses") or 1), event_id=event_id
            )
        audit(s, auth.actor_for(me), "token.create", "join_token", None, after={"role": role.value, "event": event_id, "track": track_id, "lane": lane})
        s.commit()
    except (ServiceError, ValueError) as e:
        return back("/admin/officials", getattr(e, "message", str(e)), True)
    return back("/admin/officials")


@router.post("/tokens/{token_id}/revoke")
def admin_revoke_token(token_id: int, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    t = s.get(JoinToken, token_id)
    if t:
        t.revoked = True
        s.add(t)
        audit(s, auth.actor_for(me), "token.revoke", "join_token", t.id)
        s.commit()
    return back("/admin/officials", "Code revoked.")


@router.post("/officials/{official_id}/revoke")
def admin_revoke_official(official_id: int, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    o = s.get(Official, official_id)
    if o:
        if o.id == me.id:
            return back("/admin/officials", "You can't revoke your own device here; use Sign out.", True)
        o.revoked = True
        s.add(o)
        audit(s, auth.actor_for(me), "official.revoke", "official", o.id, before={"role": o.role.value, "label": o.label})
        s.commit()
    return back("/admin/officials", "Device revoked.")


@router.get("/audit")
def admin_audit(request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    rows = s.exec(select(AuditLog).order_by(col(AuditLog.id).desc()).limit(300)).all()
    return render(request, "admin/audit.html", {"rows": rows})
