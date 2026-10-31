"""Admin roster screens: the pack roster, editing a scout and their entries,
withdrawing/disqualifying mid-event, roster import preview, and exports.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response
from sqlmodel import Session, col, select

from .. import auth
from ..models import Entry, EntryStatus, Event, EventStatus, Group, Racer, Round
from ..services import ServiceError
from ..services import export, roster
from ..services.race import RaceService
from ..core.scheduler import natural_key
from .admin import _reschedule, admin_only, back, form, get_event
from .deps import get_official, get_session, race, render

router = APIRouter(prefix="/admin")


def get_racer(s: Session, racer_id: int) -> Racer:
    r = s.get(Racer, racer_id)
    if not r:
        raise ServiceError("Scout not found", 404)
    return r


def get_entry(s: Session, entry_id: int) -> Entry:
    e = s.get(Entry, entry_id)
    if not e:
        raise ServiceError("Entry not found", 404)
    return e


def next_url(f: dict, default: str) -> str:
    url = f.get("next") or ""
    return url if url.startswith("/admin/") else default


def publish_entry(request: Request, entry: Entry) -> None:
    """Standings and the scout's page change when an entry's status does."""
    request.app.state.bus.publish(
        "entry.updated", {"entry_id": entry.id}, keys=["standings", f"event-{entry.event_id}", f"racer-{entry.racer_id}"]
    )


def den_names(s: Session) -> list[str]:
    return sorted((g.name for g in s.exec(select(Group))), key=natural_key)


# --- pack roster ---------------------------------------------------------------


@router.get("/roster")
def roster_page(request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    racers = s.exec(select(Racer, Group).join(Group, Group.id == Racer.group_id, isouter=True)).all()
    entries: dict[int, list] = {}
    for e, ev in s.exec(select(Entry, Event).join(Event, Event.id == Entry.event_id).order_by(Event.id)):
        entries.setdefault(e.racer_id, []).append((e, ev))
    rows = sorted(racers, key=lambda rg: (not rg[0].active, natural_key(rg[1].name if rg[1] else "~"), rg[0].last_name, rg[0].first_name))
    return render(request, "admin/roster.html", {"rows": rows, "entries": entries, "dens": den_names(s)})


@router.post("/racers")
async def roster_add(request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    f = await form(request)
    try:
        r = roster.create_racer(s, auth.actor_for(me), f.get("first_name", ""), f.get("last_name", ""), f.get("den", ""), f.get("member_id"))
        s.commit()
    except ServiceError as e:
        s.rollback()
        return back("/admin/roster", e.message, True)
    return back(f"/admin/racer/{r.id}", f"Added {r.display_name}. Enter them in an event below.")


@router.get("/racer/{racer_id}")
def racer_page(racer_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    r = get_racer(s, racer_id)
    group = s.get(Group, r.group_id) if r.group_id else None
    entries = s.exec(select(Entry, Event).join(Event, Event.id == Entry.event_id).where(Entry.racer_id == r.id).order_by(Event.id)).all()
    raced = roster._raced_entries(s, [e.id for e, _ in entries])
    entered = {ev.id for _, ev in entries}
    events = [ev for ev in s.exec(select(Event).order_by(col(Event.id).desc())) if ev.id not in entered and ev.status != EventStatus.done]
    others = s.exec(select(Racer).where(Racer.id != r.id).order_by(Racer.first_name, Racer.last_name)).all()
    return render(request, "admin/racer.html", {
        "racer": r, "group": group, "entries": entries, "raced": raced, "events": events, "others": others,
        "dens": den_names(s), "statuses": list(EntryStatus), "next": request.query_params.get("next", ""),
    })


def _scheduled_events_for(s: Session, racer_id: int) -> list[Event]:
    return list(
        s.exec(
            select(Event)
            .join(Entry, Entry.event_id == Event.id)
            .where(Entry.racer_id == racer_id, Event.status != EventStatus.done)
            .where(col(Event.id).in_(select(Round.event_id)))
        )
    )


@router.post("/racer/{racer_id}")
async def racer_update(racer_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    r = get_racer(s, racer_id)
    f = await form(request)
    url = next_url(f, f"/admin/racer/{r.id}")
    try:
        roster.update_racer(s, r, auth.actor_for(me), f.get("first_name", ""), f.get("last_name", ""), f.get("den", ""),
                            f.get("member_id"), f.get("active") == "on")
        s.commit()
    except ServiceError as e:
        s.rollback()
        return back(url, e.message, True)
    note = ""
    if _scheduled_events_for(s, r.id):
        note = " If the den or active status changed, regenerate the schedule of any event in progress."
    return back(url, f"Saved {r.display_name}." + note)


@router.post("/racer/{racer_id}/merge")
async def racer_merge(racer_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    src = get_racer(s, racer_id)
    f = await form(request)
    try:
        target = get_racer(s, int(f.get("target_id") or 0))
        roster.merge_racers(s, src, target, auth.actor_for(me))
        s.commit()
    except (ServiceError, ValueError) as e:
        s.rollback()
        return back(f"/admin/racer/{racer_id}", getattr(e, "message", "Pick a scout to merge into."), True)
    return back(f"/admin/racer/{target.id}", "Merged. Check the name, den and Member ID are right.")


@router.post("/racer/{racer_id}/delete")
async def racer_delete(racer_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    r = get_racer(s, racer_id)
    try:
        roster.delete_racer(s, r, auth.actor_for(me))
        s.commit()
    except ServiceError as e:
        s.rollback()
        return back(f"/admin/racer/{racer_id}", e.message, True)
    return back("/admin/roster", f"Deleted {r.display_name}.")


@router.post("/racer/{racer_id}/enter")
async def racer_enter(racer_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    r = get_racer(s, racer_id)
    f = await form(request)
    url = f"/admin/racer/{r.id}"
    try:
        ev = get_event(s, int(f.get("event_id") or 0))
        num = (f.get("number") or "").strip()
        roster.add_entry(s, ev, r, int(num) if num else None, f.get("car_name") or None, f.get("checked_in") == "on")
        s.commit()
    except ValueError:
        s.rollback()
        return back(url, "Car number must be a whole number.", True)
    except ServiceError as e:
        s.rollback()
        return back(url, e.message, True)
    return back(url, f"Entered in {ev.name}. Check-in and the schedule work as for any late arrival.")


# --- entries -------------------------------------------------------------------


@router.post("/entry/{entry_id}")
async def entry_update(entry_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    e = get_entry(s, entry_id)
    f = await form(request)
    url = next_url(f, f"/admin/racer/{e.racer_id}")
    try:
        roster.update_entry(s, e, auth.actor_for(me), int(f.get("number") or e.number), f.get("car_name"))
        s.commit()
    except ValueError:
        s.rollback()
        return back(url, "Car number must be a whole number.", True)
    except ServiceError as ex:
        s.rollback()
        return back(url, ex.message, True)
    publish_entry(request, e)
    return back(url, "Entry saved.")


@router.post("/entry/{entry_id}/status")
async def entry_status(entry_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    """Withdraw, disqualify or reinstate. Unraced heats are rescheduled right away."""
    admin_only(me)
    e = get_entry(s, entry_id)
    f = await form(request)
    url = next_url(f, f"/admin/racer/{e.racer_id}")
    try:
        status = EntryStatus(f["status"]) if f.get("status") else None
        roster.set_entry_status(s, e, status, f.get("reason") or "", auth.actor_for(me))
        s.commit()
    except (ValueError, KeyError):
        s.rollback()
        return back(url, "Unknown status.", True)
    except ServiceError as ex:
        s.rollback()
        return back(url, ex.message, True)
    racer = s.get(Racer, e.racer_id)
    what = {None: "reinstated", EntryStatus.withdrawn: "withdrawn", EntryStatus.disqualified: "disqualified"}[status]
    note = _reschedule(s, get_event(s, e.event_id), me, f"#{e.number} {racer.display_name} {what}")
    await rs.sync_timers()
    publish_entry(request, e)
    return back(url, f"#{e.number} {racer.display_name} {what}." + note)


@router.post("/entry/{entry_id}/remove")
async def entry_remove(entry_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official), rs: RaceService = Depends(race)):
    admin_only(me)
    e = get_entry(s, entry_id)
    f = await form(request)
    url = next_url(f, f"/admin/racer/{e.racer_id}")
    ev = get_event(s, e.event_id)
    try:
        roster.remove_entry(s, e, auth.actor_for(me))
        s.commit()
    except ServiceError as ex:
        s.rollback()
        return back(url, ex.message, True)
    note = _reschedule(s, ev, me, f"#{e.number} removed from the event")
    await rs.sync_timers()
    publish_entry(request, e)
    return back(url, f"Removed #{e.number} from {ev.name}." + note)


# --- import with preview -------------------------------------------------------


@router.post("/event/{event_id}/import")
async def admin_import(event_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    """First post: show what the import would change. Posting again with
    ``apply`` carries it out."""
    admin_only(me)
    ev = get_event(s, event_id)
    f = await form(request)
    text = f.get("csv_text") or ""
    upload = f.get("csv_file")
    if upload is not None and hasattr(upload, "read"):
        data = await upload.read()
        if data:
            text = data.decode("utf-8-sig", errors="replace")
    checked_in = f.get("checked_in") == "on"
    url = f"/admin/event/{ev.id}"
    try:
        plan = roster.plan_import(s, ev, text)
        if not f.get("apply"):
            return render(request, "admin/import_preview.html", {"event": ev, "plan": plan, "text": text, "checked_in": checked_in})
        result = roster.apply_import(s, ev, plan, auth.actor_for(me), checked_in)
        s.commit()
    except ServiceError as e:
        s.rollback()
        return back(url, e.message, True)
    return back(url, result.message)


# --- exports -------------------------------------------------------------------


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _csv_response(text: str, filename: str) -> Response:
    # BOM so Excel opens UTF-8 names correctly
    return Response("﻿" + text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name).strip("-").lower() or "event"


@router.get("/backup")
def admin_backup(request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    name = f"racemgr-{_stamp()}.db"
    dest = export.backup_db(request.app.state.engine, request.app.state.settings.data_dir / "backups" / name)
    return FileResponse(dest, media_type="application/vnd.sqlite3", filename=name)


@router.get("/roster.csv")
def admin_roster_csv(request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    return _csv_response(export.roster_csv(s), f"roster-{_stamp()}.csv")


@router.get("/event/{event_id}/export/{kind}.csv")
def admin_event_csv(event_id: int, kind: str, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    admin_only(me)
    ev = get_event(s, event_id)
    makers = {"roster": lambda: export.roster_csv(s, ev), "standings": lambda: export.standings_csv(s, ev), "heats": lambda: export.heats_csv(s, ev)}
    if kind not in makers:
        raise ServiceError("Unknown export", 404)
    return _csv_response(makers[kind](), f"{_slug(ev.name)}-{kind}-{_stamp()}.csv")
