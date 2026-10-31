"""JSON / HTMX action endpoints for officials, plus small read APIs."""
from __future__ import annotations

import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from .. import auth
from ..models import Entry, Event, Group, Heat, InspectionStatus, Official, Racer, Track
from ..services import ServiceError, views
from ..services.audit import audit
from ..services.race import RaceService
from .deps import get_official, get_session, public_cached, race, render, require

router = APIRouter(prefix="/api")


def done() -> Response:
    """HTMX success: no body; tell the page to refresh its live panels."""
    return Response(status_code=204, headers={"HX-Trigger": "rm-refresh"})


def parse_time(value: str | None, lane: int | None = None) -> float | None:
    """'3.123' -> 3.123; 'dnf' -> None. Blank is an error."""
    v = (value or "").strip().lower()
    where = f"Lane {lane}: " if lane else ""
    if v in ("dnf", "x", "-"):
        return None
    if not v:
        raise ServiceError(f"{where}enter a time or DNF.")
    try:
        t = float(v)
    except ValueError:
        raise ServiceError(f"{where}{value!r} is not a time.")
    if t <= 0:
        raise ServiceError(f"{where}time must be positive.")
    return t


async def form(request: Request) -> dict[str, str]:
    return {k: str(v) for k, v in (await request.form()).items()}


def heat_track(s: Session, heat_id: int) -> int:
    h = s.get(Heat, heat_id)
    if not h:
        raise ServiceError("Heat not found", 404)
    return h.track_id


# --- track operator ---------------------------------------------------------------


@router.post("/track/{track_id}/stage")
async def stage(track_id: int, request: Request, me: Official | None = Depends(get_official), rs: RaceService = Depends(race)):
    require(auth.can_operate(me, track_id), "this track")
    f = await form(request)
    heat_id = int(f["heat_id"]) if f.get("heat_id") else None
    await rs.stage(track_id, heat_id, auth.actor_for(me))
    return done()


@router.post("/track/{track_id}/unstage")
async def unstage(track_id: int, me: Official | None = Depends(get_official), rs: RaceService = Depends(race)):
    require(auth.can_operate(me, track_id), "this track")
    await rs.unstage(track_id, auth.actor_for(me))
    return done()


@router.post("/track/{track_id}/disarm")
async def disarm(track_id: int, me: Official | None = Depends(get_official), rs: RaceService = Depends(race)):
    """Armed -> staged (bad start etc.); lane phones reset."""
    require(auth.can_operate(me, track_id), "this track")
    await rs.disarm(track_id, auth.actor_for(me))
    return done()


@router.post("/track/{track_id}/arm")
async def arm(track_id: int, me: Official | None = Depends(get_official), rs: RaceService = Depends(race)):
    require(auth.can_operate(me, track_id), "this track")
    await rs.arm(track_id, auth.actor_for(me))
    return done()


@router.post("/track/{track_id}/manual")
async def manual(track_id: int, request: Request, s: Session = Depends(get_session), me: Official | None = Depends(get_official), rs: RaceService = Depends(race)):
    require(auth.can_operate(me, track_id), "this track")
    f = await form(request)
    heat = rs.current_heat(s, track_id)
    if not heat:
        raise ServiceError("No staged or armed heat on this track.", 409)
    times = {}
    for hl in rs._lanes(s, heat.id):
        if hl.entry_id:
            times[hl.lane] = parse_time(f.get(f"lane_{hl.lane}"), hl.lane)
    await rs.manual_entry(track_id, times, auth.actor_for(me))
    return done()


@router.get("/track/{track_id}/state")
def track_state(track_id: int, s: Session = Depends(get_session), rs: RaceService = Depends(race)):
    t = s.get(Track, track_id)
    if not t:
        raise ServiceError("Track not found", 404)
    return views.track_state(s, t, rs.timer_status(t.id))


# --- lane official -------------------------------------------------------------------


class LaneTime(BaseModel):
    heat_id: int
    # Preferred: STOP press on the server clock; the time is stop minus the
    # heat's shared start. Fallback: a locally timed elapsed time. Neither = DNF.
    stop_ms: float | None = None
    elapsed_ms: float | None = None
    start_ms: float | None = Field(default=None, description="the official's own START press, if any")
    submission_id: str = Field(min_length=4, max_length=64)


@router.post("/track/{track_id}/lane/{lane}/time")
async def lane_time(track_id: int, lane: int, body: LaneTime, me: Official | None = Depends(get_official), rs: RaceService = Depends(race)):
    require(auth.can_time_lane(me, track_id, lane), "this lane")
    seconds = None if body.elapsed_ms is None else round(body.elapsed_ms / 1000.0, 3)
    return await rs.submit_lane(
        track_id, lane, body.heat_id, seconds, body.submission_id, auth.actor_for(me), stop_ms=body.stop_ms, start_ms=body.start_ms
    )


class StartPress(BaseModel):
    heat_id: int
    start_ms: float


@router.post("/track/{track_id}/start")
async def start_heat(track_id: int, body: StartPress, me: Official | None = Depends(get_official), rs: RaceService = Depends(race)):
    """START press from the operator or a lane official; the earliest one counts."""
    require(auth.can_start(me, track_id), "starting heats on this track")
    who = f"Lane {me.lane}" if me.role.value == "lane_official" else auth.ROLE_LABELS[me.role]
    return await rs.register_start(track_id, body.heat_id, body.start_ms, who, auth.actor_for(me))


@router.get("/time")
async def server_time():
    """Server clock for phones to sync against (epoch milliseconds).

    Async on purpose: answered straight from the event loop, not queued
    behind page renders in the threadpool, so the round trip (and thus the
    phone's clock error) stays small when the server is busy."""
    return {"now": time.time() * 1000}


# --- head timekeeper --------------------------------------------------------------------


@router.post("/heat/{heat_id}/approve")
async def approve(heat_id: int, me: Official | None = Depends(get_official), rs: RaceService = Depends(race)):
    require(auth.can_timekeep(me), "approving results")
    await rs.approve(heat_id, auth.actor_for(me))
    return done()


@router.post("/heat/{heat_id}/edit")
async def edit(heat_id: int, request: Request, me: Official | None = Depends(get_official), rs: RaceService = Depends(race)):
    require(auth.can_timekeep(me), "editing results")
    f = await form(request)
    lane = int(f.get("lane", "0"))
    await rs.edit_lane(heat_id, lane, parse_time(f.get("time"), lane), f.get("reason", ""), auth.actor_for(me))
    return done()


@router.post("/heat/{heat_id}/rerun")
async def rerun(heat_id: int, request: Request, s: Session = Depends(get_session), me: Official | None = Depends(get_official), rs: RaceService = Depends(race)):
    track_id = heat_track(s, heat_id)
    # operators may call a re-run on their own track before results are in
    require(auth.can_timekeep(me) or auth.can_operate(me, track_id), "re-running heats")
    f = await form(request)
    reason = (f.get("reason") or "").strip()
    if reason == "__other":
        reason = (f.get("reason_other") or "").strip()
    if not reason:
        raise ServiceError("Pick a reason for the re-run.")
    await rs.rerun(heat_id, reason, auth.actor_for(me))
    return done()


# --- check-in -------------------------------------------------------------------------------


@router.post("/entry/{entry_id}/checkin")
async def checkin(entry_id: int, request: Request, s: Session = Depends(get_session), me: Official | None = Depends(get_official)):
    require(auth.can_checkin(me), "check-in")
    entry = s.get(Entry, entry_id)
    if not entry:
        raise ServiceError("Entry not found", 404)
    f = await form(request)
    before = {"checked_in": entry.checked_in, "weight_oz": entry.weight_oz, "inspection": entry.inspection_status.value}
    entry.checked_in = f.get("checked_in") == "on"
    w = f.get("weight_oz", "").strip()
    try:
        entry.weight_oz = float(w) if w else None
    except ValueError:
        raise ServiceError(f"{w!r} is not a weight.")
    entry.inspection_status = InspectionStatus(f.get("inspection_status", entry.inspection_status.value))
    entry.inspection_notes = f.get("inspection_notes", "").strip() or None
    s.add(entry)
    after = {"checked_in": entry.checked_in, "weight_oz": entry.weight_oz, "inspection": entry.inspection_status.value}
    audit(s, auth.actor_for(me), "entry.checkin", "entry", entry.id, before=before, after=after)
    s.commit()
    request.app.state.bus.publish("entry.updated", {"entry_id": entry.id, **after}, keys=["checkin", f"event-{entry.event_id}"])
    racer = s.get(Racer, entry.racer_id)
    group = s.get(Group, racer.group_id) if racer.group_id else None
    ev = s.get(Event, entry.event_id)
    return render(request, "partials/checkin_row.html", {"e": entry, "r": racer, "g": group, "event": ev, "saved": True})


# Check-in photos. kind "entry" is the car or boat; it maps to an Entry column
# holding a file name under <data_dir>/photos. Scouts themselves aren't photographed.
PHOTO_FIELDS = {"entry": "photo_path"}
PHOTO_MAX_BYTES = 10 * 1024 * 1024
PHOTO_TYPES = {b"\xff\xd8\xff": (".jpg", "image/jpeg"), b"\x89PNG": (".png", "image/png"), b"RIFF": (".webp", "image/webp")}


def photo_dir(request: Request) -> Path:
    return Path(request.app.state.settings.data_dir) / "photos"


def photo_entry(s: Session, entry_id: int, kind: str) -> tuple[Entry, str]:
    if kind not in PHOTO_FIELDS:
        raise ServiceError("Unknown photo kind", 404)
    entry = s.get(Entry, entry_id)
    if not entry:
        raise ServiceError("Entry not found", 404)
    return entry, PHOTO_FIELDS[kind]


def sniff_image(data: bytes) -> tuple[str, str] | None:
    for magic, (ext, mime) in PHOTO_TYPES.items():
        if data.startswith(magic) and (magic != b"RIFF" or data[8:12] == b"WEBP"):
            return ext, mime
    return None


@router.post("/entry/{entry_id}/photo/{kind}")
async def upload_photo(entry_id: int, kind: str, request: Request, s: Session = Depends(get_session), me: Official | None = Depends(get_official)):
    require(auth.can_checkin(me), "check-in")
    entry, field = photo_entry(s, entry_id, kind)
    upload = (await request.form()).get("photo")
    if upload is None or isinstance(upload, str):
        raise ServiceError("No photo attached.")
    data = await upload.read(PHOTO_MAX_BYTES + 1)
    if len(data) > PHOTO_MAX_BYTES:
        raise ServiceError("That photo is too large (10 MB max).", 413)
    sniffed = sniff_image(data)
    if not sniffed:
        raise ServiceError("That file isn't a JPEG, PNG or WebP photo.")
    folder = photo_dir(request)
    folder.mkdir(parents=True, exist_ok=True)
    # a fresh name each time so phones never show a cached old photo
    name = f"entry{entry.id}-{kind}-{secrets.token_hex(4)}{sniffed[0]}"
    (folder / name).write_bytes(data)
    old = getattr(entry, field)
    setattr(entry, field, name)
    s.add(entry)
    audit(s, auth.actor_for(me), f"entry.photo.{kind}", "entry", entry.id, before={field: old}, after={field: name})
    s.commit()
    if old:
        (folder / old).unlink(missing_ok=True)
    return {"url": f"/api/entry/{entry.id}/photo/{kind}?v={name}"}


@router.post("/entry/{entry_id}/photo/{kind}/delete")
def delete_photo(entry_id: int, kind: str, request: Request, s: Session = Depends(get_session), me: Official | None = Depends(get_official)):
    require(auth.can_checkin(me), "check-in")
    entry, field = photo_entry(s, entry_id, kind)
    old = getattr(entry, field)
    if old:
        setattr(entry, field, None)
        s.add(entry)
        audit(s, auth.actor_for(me), f"entry.photo.{kind}", "entry", entry.id, before={field: old}, after={field: None})
        s.commit()
        (photo_dir(request) / old).unlink(missing_ok=True)
    return {"url": None}


@router.get("/entry/{entry_id}/photo/{kind}")
def get_photo(entry_id: int, kind: str, request: Request, s: Session = Depends(get_session)):
    entry, field = photo_entry(s, entry_id, kind)
    name = getattr(entry, field)
    path = photo_dir(request) / name if name else None
    if not path or not path.is_file():
        raise ServiceError("No photo yet", 404)
    mime = next(m for ext, m in PHOTO_TYPES.values() if path.suffix == ext)
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})


# --- public read APIs -------------------------------------------------------------------------


@router.get("/racers")
@public_cached
def racers(request: Request, ids: str = "", s: Session = Depends(get_session), rs: RaceService = Depends(race)):
    """Followed-racer info + current status, for the public follow panel."""
    try:
        wanted = {int(x) for x in ids.split(",") if x.strip()}
    except ValueError:
        raise ServiceError("ids must be comma-separated numbers")
    if not wanted:
        return {"racers": []}
    rows = s.exec(
        select(Racer, Group).join(Group, Group.id == Racer.group_id, isouter=True).where(col(Racer.id).in_(wanted))
    ).all()
    # one computation per data change, shared by every phone's follow panel
    status = request.app.state.page_cache.get("follow-status", lambda: rs.alerts.status_for(s))
    return {
        "racers": [
            {"id": r.id, "name": r.display_name, "group": g.name if g else "", "status": status.get(r.id)}
            for r, g in rows
        ]
    }
